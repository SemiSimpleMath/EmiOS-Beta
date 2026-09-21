"""Execution spans, cooperative cancellation, and work-attempt ownership.

No model calls or status-file writes occur here. A call admitted before cancellation
can finish; it stays visible until its actual exit. Context must be copied explicitly
when spawning threads (``copy_context().run``).
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import os
import threading
import uuid

PROCESS_ID = f"{os.getpid()}:{uuid.uuid4().hex}"
_CURRENT = ContextVar("execution_span", default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


class ExecutionCancelled(RuntimeError):
    """Execution was revoked; do not interpret this as permission to retry."""


@dataclass(frozen=True)
class Owner:
    store: object = field(repr=False, compare=True)
    work_id: str
    main_node_id: str
    dispatch_epoch: int

    def public(self):
        return dict(work_id=self.work_id, main_node_id=self.main_node_id,
                    dispatch_epoch=self.dispatch_epoch)


@dataclass
class Span:
    id: str
    kind: str
    name: str
    parent: object = None
    owner: Owner | None = None
    attribution_node_id: str | None = None
    entered_at: str = field(default_factory=now)
    cancel_requested_at: str | None = None
    cancel_observed_at: str | None = None
    cancel_reason: str | None = None
    exited_at: str | None = None
    outcome: str | None = None
    recovery: bool = False
    activity: str | None = None
    last_progress_at: str = field(default_factory=now)


class ExecutionRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = {}
        self.recent = deque(maxlen=256)
        self.pending_finish = {}
        self.sessions = {}
        self.sessions_lock = threading.Lock()

    def _check(self, span):
        cursor = span
        while cursor:
            if cursor.cancel_requested_at:
                span.cancel_observed_at = span.cancel_observed_at or now()
                raise ExecutionCancelled(cursor.cancel_reason or "cancelled")
            cursor = cursor.parent

    def check(self):
        current = _CURRENT.get()
        if current:
            with self.lock:
                self._check(current)
            if current.owner and not current.recovery and current.owner.store.execution_revoked(current.owner):
                self.cancel_owner(current.owner, "work attempt revoked")
                with self.lock:
                    self._check(current)

    def cancel(self, span_id, reason="cancelled by operator"):
        with self.lock:
            span = self.active.get(span_id)
            if span is None:
                # Exited parents remain reachable through still-running descendants.
                for child in self.active.values():
                    cursor = child.parent
                    while cursor:
                        if cursor.id == span_id:
                            span = cursor
                            break
                        cursor = cursor.parent
                    if span:
                        break
            if span is None:
                return False
            span.cancel_requested_at = span.cancel_requested_at or now()
            span.cancel_reason = reason
            return True

    def cancel_owner(self, owner, reason):
        with self.lock:
            for span in self.active.values():
                if span.owner == owner:
                    span.cancel_requested_at = span.cancel_requested_at or now()
                    span.cancel_reason = reason

    @contextmanager
    def span(self, kind, name, *, span_id=None, owner=None, attribution=None, recovery=False):
        self.check()
        parent = _CURRENT.get()
        owner = owner or (parent.owner if parent else None)
        if attribution is None:
            from work_objects.runtime import peek_work_context
            ctx = peek_work_context()
            attribution = ctx.node_id if ctx else (parent.attribution_node_id if parent else None)
            owner = owner or (ctx.owner if ctx else None)
        item = Span(span_id or uuid.uuid4().hex, kind, name, parent, owner, attribution)
        item.recovery = recovery or bool(parent and parent.recovery)
        with self.lock:
            if parent:
                self._check(parent)
            self.active[item.id] = item
            if parent:
                parent.last_progress_at = item.entered_at
        token = _CURRENT.set(item)
        try:
            yield item
            item.outcome = "returned"
        except BaseException:
            item.outcome = "interrupted"
            raise
        finally:
            _CURRENT.reset(token)
            with self.lock:
                item.exited_at = now()
                if parent:
                    parent.last_progress_at = item.exited_at
                self.active.pop(item.id, None)
                self.recent.append(self._public(item))
                finish = self.pending_finish.get(item.owner)
                if finish and not any(s.owner == item.owner for s in self.active.values()):
                    self.pending_finish.pop(item.owner, None)
                else:
                    finish = None
            if finish:
                owner, callback = finish
                owner.store.finish_execution(owner)
                if callback:
                    callback()

    def _public(self, s):
        root = s
        while root.parent:
            root = root.parent
        cursor, cancelling = s, False
        while cursor:
            cancelling |= bool(cursor.cancel_requested_at)
            cursor = cursor.parent
        return dict(id=s.id, kind=s.kind, name=s.name,
                    parent_id=s.parent.id if s.parent else None, root_id=root.id,
                    owner=s.owner.public() if s.owner else None,
                    attribution_node_id=s.attribution_node_id, entered_at=s.entered_at,
                    activity=s.activity or ("waiting_for_child" if any(child.parent is s for child in self.active.values()) else s.kind),
                    last_progress_at=s.last_progress_at,
                    state="exited" if s.exited_at else ("cancelling" if cancelling else "running"),
                    cancel_requested_at=s.cancel_requested_at, cancel_observed_at=s.cancel_observed_at,
                    cancel_reason=s.cancel_reason, exited_at=s.exited_at, outcome=s.outcome)

    def finish_owner(self, owner, on_finished=None):
        with self.lock:
            if any(s.owner == owner for s in self.active.values()):
                self.pending_finish[owner] = (owner, on_finished)
                return
        owner.store.finish_execution(owner)
        if on_finished:
            on_finished()

    def snapshot(self):
        with self.lock:
            return dict(process_instance_id=PROCESS_ID,
                        active=[self._public(s) for s in self.active.values()],
                        recent=list(self.recent))


# One process registry exposed by MAMInstanceManager; context is shared by all
# standard invokers, including direct agent calls outside a manager.
REGISTRY = ExecutionRegistry()


def current_span():
    return _CURRENT.get()


def current_owner():
    current = _CURRENT.get()
    return current.owner if current else None


def agent_activation(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        current = _CURRENT.get()
        # An override calling super is one activation, not two.
        if current and current.kind == "agent" and getattr(current, "agent_instance", None) is self:
            return fn(self, *args, **kwargs)
        with REGISTRY.span("agent", getattr(self, "name", type(self).__name__)) as span:
            span.agent_instance = self
            return fn(self, *args, **kwargs)
    return wrapped


def model_boundary(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        REGISTRY.check()
        result = fn(*args, **kwargs)
        REGISTRY.check()  # Before any overridden result applier/reconciliation.
        return result
    return wrapped


@contextmanager
def tool_call(name, *, external=True):
    with REGISTRY.span("tool", name) as span:
        owner = span.owner
        # Cancellation and admission share a lock. SQLite admission also checks
        # durable revocation atomically with writing the receipt.
        with REGISTRY.lock:
            REGISTRY._check(span)
            if owner:
                owner.store.admit_execution_call(owner, span.id, name, external)
        try:
            yield span
        except BaseException as exc:
            if owner:
                owner.store.finish_execution_call(span.id, "unknown" if external else "settled", str(exc))
            raise
        else:
            if owner:
                result = getattr(span, "result", None)
                detail = str(getattr(result, "content", None) or result or "returned")
                data = getattr(result, "data", None) or {}
                uncertain = (hasattr(span, "result") and result is None) or (
                    isinstance(data, dict) and (data.get("outcome_unknown") is True
                    or data.get("error_code") == "mcp_call_failed"))
                owner.store.finish_execution_call(span.id, "unknown" if external and uncertain else "settled", detail)


def activity(kind):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with REGISTRY.span(kind, fn.__name__):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def bind_background(fn, name):
    """Reserve before queuing so a parent exit cannot hide a queued descendant.

    Returns the callable and a cleanup callback for failed/cancelled submission.
    Non-execution infrastructure threads keep their existing cheap path.
    """
    from contextvars import copy_context
    if _CURRENT.get() is None:
        return fn, lambda: None
    context = copy_context()
    scope = REGISTRY.span("background", name)
    item = context.run(scope.__enter__)
    with REGISTRY.lock:
        item.activity = "queued"
    once = threading.Lock()
    closed = False
    def close():
        nonlocal closed
        with once:
            if not closed:
                closed = True
                context.run(scope.__exit__, None, None, None)
    def invoke():
        try:
            def run():
                with REGISTRY.lock:
                    item.activity = "running"
                    item.last_progress_at = now()
                REGISTRY.check()
                return fn()
            return context.run(run)
        finally:
            close()
    return invoke, close
