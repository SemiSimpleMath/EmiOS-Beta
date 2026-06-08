"""Mailbox + MailboxDispatcher.

Typed message bus for delivering messages into a running manager.

A manager runs on its own thread inside ``MultiAgentManager._run_loop``.
Anything outside that thread (an HTTP route, an @mention parser at ingress,
another manager, a periodic monitor) needs a safe way to deliver a message
that the manager will see at its next cycle boundary — never mid-LLM-call.

The mailbox is the channel. Senders post typed messages; the manager drains
them at the top of each cycle and dispatches by ``message_type``.

Built-in message types (interpreted by the manager's drain handler):

  ``agent_inject``    payload = ``{role_or_name: text, ...}``
                      Append ``text`` to the runtime-injection slot of the
                      agent identified by ``role_or_name`` (resolved through
                      the manager's role bindings, then by direct name).
                      The agent renders its own slot during prompt assembly.

  ``blackboard_write`` payload = ``{key: value, ...}``
                      ``blackboard.update_state_value(key, value)`` for each.
                      Useful for setting flags like ``cancelled`` from outside.

New types can be added later (pause / resume / replan / etc.) without changing
the mailbox itself — only the manager's drain handler grows new cases.

Senders own message wrapping. The mailbox stores the payload verbatim. For
``agent_inject`` text, the sender is responsible for any user-facing framing
(``+++++ Latest instruction from user +++ User: <body> +++++``); the manager
only routes.

In-memory only. Per-invocation queues live as long as the process. Stale
messages are dropped at drain time via TTL so a message that arrived after
the manager already exited doesn't get applied to a later same-named-but-
different invocation by accident.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Default TTL: messages older than this are dropped at drain time.
# Tuned long enough that a slow tool call completing doesn't lose a steering
# message; short enough that a message from many minutes ago doesn't quietly
# apply to a subsequent unrelated cycle.
_DEFAULT_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class MailboxMessage:
    """One message destined for a manager invocation.

    ``message_type`` selects the manager's handler. ``payload`` is opaque
    to the mailbox itself; semantics are agreed between sender and the
    manager's drain handler.

    ``from_who`` is informational only — useful for logging and for
    rendering ``+++ from: user +++`` style headers if a sender chooses to.
    """
    message_type: str
    payload: Dict[str, Any]
    posted_at_utc: datetime
    from_who: str = "system"
    metadata: Dict[str, Any] = field(default_factory=dict)


class Mailbox:
    """Per-invocation typed-message inbox. Singleton via ``DI.mailbox``.

    Keyed by ``invocation_id`` (the same id ``ManagerInvoker`` uses in its
    active-invocations registry). Posting to an unknown invocation_id is
    harmless — the queue just lingers and ages out.
    """

    def __init__(self, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._queues: Dict[str, Deque[MailboxMessage]] = {}
        self._ttl_seconds = float(ttl_seconds)
        logger.info("✅ Mailbox initialized (ttl=%.1fs).", ttl_seconds)

    def post(
        self,
        *,
        invocation_id: str,
        message_type: str,
        payload: Dict[str, Any],
        from_who: str = "system",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        """Drop one message into an invocation's queue.

        Returns True if posted, False on bad input. Does NOT verify the
        invocation is still running (that's the sender's concern).
        """
        invocation_id = (invocation_id or "").strip()
        message_type = (message_type or "").strip()
        if not invocation_id or not message_type:
            return False
        if not isinstance(payload, dict):
            return False
        msg = MailboxMessage(
            message_type=message_type,
            payload=dict(payload),
            posted_at_utc=datetime.now(timezone.utc),
            from_who=str(from_who or "system"),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
        with self._lock:
            self._queues.setdefault(invocation_id, deque()).append(msg)
        logger.info(
            "[mailbox] posted type=%s to %s payload_keys=%s",
            message_type, invocation_id, list(payload.keys()),
        )
        return True

    def drain(self, invocation_id: str) -> List[MailboxMessage]:
        """Pop all currently-valid messages for an invocation, in posting order.

        Stale messages (older than TTL) are silently dropped here. Returns
        an empty list when there's nothing waiting. Called by the manager
        at the top of each cycle.
        """
        invocation_id = (invocation_id or "").strip()
        if not invocation_id:
            return []
        cutoff_seconds = self._ttl_seconds
        now = datetime.now(timezone.utc)
        out: List[MailboxMessage] = []
        with self._lock:
            queue = self._queues.get(invocation_id)
            if not queue:
                return []
            while queue:
                msg = queue.popleft()
                age = (now - msg.posted_at_utc).total_seconds()
                if age > cutoff_seconds:
                    logger.info(
                        "[mailbox] dropping stale message type=%s "
                        "(age=%.1fs > ttl=%.1fs) for %s",
                        msg.message_type, age, cutoff_seconds, invocation_id,
                    )
                    continue
                out.append(msg)
            if not queue:
                self._queues.pop(invocation_id, None)
        return out

    def peek_count(self, invocation_id: str) -> int:
        """Return how many messages are queued (no TTL pruning)."""
        with self._lock:
            queue = self._queues.get(invocation_id)
            return len(queue) if queue else 0

    def clear(self, invocation_id: str) -> None:
        """Drop all queued messages for an invocation (e.g., on cancel)."""
        with self._lock:
            self._queues.pop(invocation_id, None)


# Reserved blackboard key holding per-agent runtime injection lists.
# Append-only ``{agent_name: [text, text, ...]}`` — never cleared by the
# dispatcher. Each agent renders its own slot during prompt assembly.
_RUNTIME_INJECTIONS_BB_KEY = "_runtime_injections"


class MailboxDispatcher:
    """Drain a manager's mailbox once and apply each message to its blackboard.

    A single ``drain_to(blackboard, invocation_id, role_resolver)`` call
    pulls everything queued for an invocation and writes the appropriate
    state to that manager's blackboard. The manager itself doesn't need
    to know how each message_type is interpreted — that lives here.

    Adding a new message_type is a single new ``elif`` branch in
    ``_dispatch_one``. Manager code does not change.

    The dispatcher is stateless and uses the global ``Mailbox`` singleton
    via DI; the only external dependency is the blackboard it writes to
    and the role resolver it consults to map role names (``planner``)
    to namespaced agent names (``web::planner``).
    """

    def drain_to(
        self,
        *,
        blackboard,
        invocation_id: str,
        role_resolver=None,
    ) -> int:
        """Drain everything queued for ``invocation_id`` and apply to
        ``blackboard``. Returns the number of messages dispatched.

        Never raises into the caller — failures are logged and the
        offending message dropped so a bad payload can't break the
        manager loop.
        """
        if not isinstance(invocation_id, str) or not invocation_id.strip():
            return 0
        mailbox = self._resolve_mailbox()
        if mailbox is None:
            return 0
        try:
            messages = mailbox.drain(invocation_id)
        except Exception:
            logger.debug(
                "MailboxDispatcher: drain raised for %s; skipping",
                invocation_id, exc_info=True,
            )
            return 0
        if not messages:
            return 0

        applied = 0
        for msg in messages:
            try:
                self._dispatch_one(msg, blackboard, role_resolver)
                applied += 1
            except Exception:
                logger.error(
                    "MailboxDispatcher: dispatch failed for type=%s — dropped",
                    getattr(msg, "message_type", "<?>"), exc_info=True,
                )
        return applied

    @staticmethod
    def _resolve_mailbox() -> Mailbox | None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            return getattr(DI, "mailbox", None)
        except Exception:
            return None

    def _dispatch_one(self, msg: MailboxMessage, blackboard, role_resolver) -> None:
        mtype = getattr(msg, "message_type", "")
        payload = getattr(msg, "payload", None)
        if not isinstance(payload, dict):
            return

        if mtype == "agent_inject":
            self._apply_agent_inject(
                payload, blackboard, role_resolver,
                posted_at_utc=getattr(msg, "posted_at_utc", None),
                from_who=getattr(msg, "from_who", "system"),
            )
            return

        if mtype == "blackboard_write":
            self._apply_blackboard_write(payload, blackboard)
            return

        logger.warning("MailboxDispatcher: unknown message_type=%r — dropped", mtype)

    @staticmethod
    def _apply_agent_inject(
        payload: dict, blackboard, role_resolver,
        *, posted_at_utc=None, from_who: str = "system",
    ) -> None:
        for role_or_name, content in payload.items():
            target = role_or_name
            if callable(role_resolver):
                try:
                    resolved = role_resolver(role_or_name)
                    if isinstance(resolved, str) and resolved.strip():
                        target = resolved.strip()
                except Exception:
                    logger.debug(
                        "MailboxDispatcher: role_resolver raised on %r",
                        role_or_name, exc_info=True,
                    )
            if not isinstance(target, str) or not target.strip():
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            MailboxDispatcher._append_runtime_injection(
                blackboard, target.strip(), content.strip(),
                posted_at_utc=posted_at_utc, from_who=from_who,
            )

    @staticmethod
    def _apply_blackboard_write(payload: dict, blackboard) -> None:
        for key, value in payload.items():
            if not isinstance(key, str) or not key.strip():
                continue
            blackboard.update_state_value(key.strip(), value)

    @staticmethod
    def _append_runtime_injection(
        blackboard, agent_name: str, text: str,
        *, posted_at_utc=None, from_who: str = "system",
    ) -> None:
        """Append one entry to the named agent's runtime-injection slot on
        ``blackboard``. Append-only — accumulates like chat history. Each entry
        keeps its arrival time + sender so prompt assembly can render it
        time-ordered with precedence (a later message supersedes earlier ones;
        a user message outranks system/agent). Agent prompt assembly renders its
        own slot during each activation.
        """
        store = blackboard.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) or {}
        if not isinstance(store, dict):
            store = {}
        existing = store.get(agent_name)
        if not isinstance(existing, list):
            existing = []
        entry = {
            "text": text,
            "posted_at_utc": posted_at_utc.isoformat() if hasattr(posted_at_utc, "isoformat") else posted_at_utc,
            "from_who": (from_who or "system"),
        }
        existing.append(entry)
        store[agent_name] = existing
        blackboard.update_state_value(_RUNTIME_INJECTIONS_BB_KEY, store)
        logger.info(
            "runtime_injection appended for agent=%s from=%s len=%d (total=%d)",
            agent_name, entry["from_who"], len(text), len(existing),
        )
