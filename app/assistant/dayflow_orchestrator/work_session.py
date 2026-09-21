"""WorkSession — a copy of the orchestrator room, open until its call returns.

The single dispatch host for dayflow work nodes (work-session rewrite,
2026-08-04). Every trigger (planning tick, scheduler time-wake) converges here:

  open_session(store, work_id, node_id, delegate_to)

Each session runs dayflow_dispatch_manager: build arguments, call the selected tool,
record its result, then invoke work_finalizer_node. The tool may wait for the user
or run a worker; both use the dayflow room's scope and the same completion path.

The dispatching tick returns immediately in both cases; the GRAPH is the return
channel. The ticket branch used to be the exception — it surfaced a ticket by
hand, parked the node on a ``user_reply`` wake and returned nothing, so the
user's answer had to be reconstructed later from the ticket store.

Authority is DERIVED, never minted: the scope is the room's own ``scope.yaml``
loaded through the standard loader (pods ``[all]``, authority 95, KG writes —
exactly what every other consumer of the room gets), so a worker sees precisely
what the orchestrator sees. The 2026-08-03 forward-email flounder — a worker
blind to an email pod its goal referenced — is structurally impossible here.

Session registration belongs to the execution registry. Durable attempts prevent
replacement dispatch while old execution is running or an external outcome is unknown.
Timeout records a failed result and revokes further calls/writes; it does not kill a
thread. Existing ticket recovery reconnects the original question after restart.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_ROOM_ID = "dayflow_orchestrator"
_SESSION_GRACE_SECONDS = 60

from app.assistant.manager_runtime.execution import REGISTRY

_sessions_lock = REGISTRY.sessions_lock
_live_sessions: dict = REGISTRY.sessions   # session_id -> {"thread": Thread, "started_at": datetime}


def session_id_for(work_id: str, node_id: str) -> str:
    return f"work_session::{work_id}::{node_id}"


def session_alive_by_id(sid: str, expected_epoch=None) -> bool:
    """True if the named session has a live thread (or registered within the grace window)."""
    with _sessions_lock:
        entry = _live_sessions.get(sid)
    if entry is None or (expected_epoch is not None and entry.get("epoch") != expected_epoch):
        return False
    if entry["thread"].is_alive():
        return True
    age = (datetime.now(timezone.utc) - entry["started_at"]).total_seconds()
    return age < _SESSION_GRACE_SECONDS


def session_alive(work_id: str, node_id: str, expected_epoch=None) -> bool:
    """True if a live session owns this node directly (its own session id)."""
    return session_alive_by_id(session_id_for(work_id, node_id), expected_epoch)


def session_started_at_by_id(sid: str):
    with _sessions_lock:
        entry = _live_sessions.get(sid)
    return entry["started_at"] if entry else None


def session_started_at(work_id: str, node_id: str):
    """The session's start time, or None when no session is registered."""
    return session_started_at_by_id(session_id_for(work_id, node_id))


def room_session_scope(work_id: str, node_id: str):
    """The dayflow room's own permission scope with this session's identity stamped on it.
    One loader, one source of truth (rooms/dayflow_orchestrator/scope.yaml) — the same
    effective permissions every other consumer of the room resolves."""
    from app.assistant.scope.loader import load_scope_for_source
    scope = load_scope_for_source(
        kind="room", source_id=_ROOM_ID, actor_id="work_session",
        identity_overrides={
            "surface": "system",
            "room_id": _ROOM_ID,
            "scope_id": session_id_for(work_id, node_id),
        },
    )
    if scope is None:
        raise ValueError(f"work_session: room scope for {_ROOM_ID!r} did not resolve")
    return scope


def _start_registered_thread(sid, thread, epoch, before_start=None):
    with _sessions_lock:
        current = _live_sessions.get(sid)
        if current is not None and (current.get("epoch") == epoch or current["thread"].is_alive()):
            raise ValueError("dispatch attempt already has a registered session")
        _live_sessions[sid] = {"thread": thread, "started_at": datetime.now(timezone.utc), "epoch": epoch}
    try:
        if before_start is not None:
            before_start()
        thread.start()
    except Exception:
        with _sessions_lock:
            if (_live_sessions.get(sid) or {}).get("thread") is thread:
                _live_sessions.pop(sid, None)
        raise


def open_session(store, work_id: str, node_id: str, delegate_to: str, *, expected_epoch=None) -> None:
    """Carry out the switchboard's routing decision for one node — the ONE dispatch path.

    Register the session before starting its thread so boot recovery can see its owner.
    The session thread runs the dispatch manager; the dispatching pass returns immediately."""
    wo = store.load(work_id)
    node = wo.nodes.get(node_id)
    if node is None:
        return
    if not wo.is_work_unit(node):
        raise ValueError("work_session: worker provenance is not an orchestrator assignment")

    if node.status != "dispatched":
        logger.error(
            "[work_session] refusing to run %s::%s — status %r; the dispatch gate claims a node "
            "before any tool is called", work_id, node_id, node.status,
        )
        return

    if not str(delegate_to or "").strip():
        logger.error("[work_session] refusing to run %s::%s — the switchboard named no tool",
                     work_id, node_id)
        return

    epoch = int(node.payload.get("dispatch_epoch") or 0)
    if expected_epoch is not None and int(expected_epoch) != epoch:
        raise ValueError("stale dispatch claim before session creation")
    sid = session_id_for(work_id, node_id)
    thread = threading.Thread(target=_run_dispatch_room,
                              args=(store, work_id, node_id, sid, delegate_to, epoch),
                              name=sid, daemon=True)
    def stamp_session():
        store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                   "status": node.status, "session_id": sid, "expected_dispatch_epoch": epoch},
                    actor="work_session")
    from app.assistant.manager_runtime.execution import Owner
    owner = Owner(store, work_id, node_id, epoch)
    store.start_execution(owner)
    try:
        _start_registered_thread(sid, thread, epoch, before_start=stamp_session)
    except BaseException:
        REGISTRY.finish_owner(owner)
        raise
    logger.info("[work_session] session %s started", sid)


def _record_dispatch_failure(store, work_id, node_id, epoch, detail):
    from work_objects.result_recorder import record_tool_result
    from app.assistant.utils.pydantic_classes import ToolResult
    if record_tool_result(store, work_id, node_id,
            ToolResult(result_type="error", content=detail, data={"aborted": True}),
            actor="work_session", expected_epoch=epoch):
        _finalize_recorded_result(work_id, node_id, expected_epoch=epoch)


def _run_dispatch_room(store, work_id, node_id, sid, delegate_to, my_epoch):
    from app.assistant.manager_runtime.execution import Owner, REGISTRY
    owner = Owner(store, work_id, node_id, my_epoch)
    try:
        with REGISTRY.span("attempt", sid, owner=owner, attribution=node_id):
            _run_dispatch_room_body(store, work_id, node_id, sid, delegate_to, my_epoch)
    finally:
        from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
        REGISTRY.finish_owner(owner, on_finished=lambda: signal_work_progress(f"{work_id}::{node_id}"))


def _run_dispatch_room_body(store, work_id: str, node_id: str, sid: str, delegate_to: str, my_epoch: int) -> None:
    """The session thread: open the dispatch room on this node and hold it until its call returns.

    ONE runner for every tool. There used to be two — an ask branch that called the ticket tool
    by hand and a worker branch that called discharge_node — which is the split that made asks a
    lifecycle of their own instead of a slow tool call. The room runs the same three stages the
    orchestrator's tail used to run (arguments -> tool caller -> finalizer), so what differs
    between a ticket and the work team is only which tool the switchboard named.

    Blocking here is the point. create_dayflow_ticket holds the call open for the whole ask
    window so the user's answer comes back as the tool's RESULT and lands on the graph like any
    other — that is what stops a decline being reconstructed from the ticket store after the
    steward has already closed the object. This thread can afford to wait; the orchestrator
    tick, which the scheduler admits one at a time, cannot.
    """
    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    ref = f"{work_id}::{node_id}"
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is None or node.status != "dispatched" or int(node.payload.get("dispatch_epoch") or 0) != my_epoch:
            return

        manager = DI.multi_agent_manager_factory.create_manager("dayflow_dispatch_manager")
        # The room's whole input. The arguments node reads the NODE for everything else, so
        # these two values plus the graph are the entire contract.
        manager.blackboard.update_state_value("delegate_to", str(delegate_to).strip())
        manager.blackboard.update_state_value("work_node_ref", ref)
        manager.blackboard.update_state_value("dispatch_epoch", my_epoch)
        result = DI.manager_invoker.invoke(manager, Message(
            event_topic="dayflow_dispatch",
            sender="system",
            receiver=None,
            task="",
            information="",
            content="",
            data={"work_node": ref, "delegate_to": str(delegate_to).strip(), "dispatch_epoch": my_epoch},
            scope_context=room_session_scope(work_id, node_id),
        ))
        n = store.load(work_id).nodes.get(node_id)
        if n is not None and n.status == "dispatched" and int(n.payload.get("dispatch_epoch") or 0) == my_epoch:
            from work_objects.result_recorder import _answer_text, _is_failure
            detail = _answer_text(result) if _is_failure(result) else "Dispatch manager returned without recording a tool result."
            _record_dispatch_failure(store, work_id, node_id, my_epoch, detail)
        logger.info("[work_session] %s -> %s", sid, n.status if n else "missing")
    except Exception as e:
        logger.error("[work_session] session %s crashed: %s", sid, e, exc_info=True)
        try:
            _record_dispatch_failure(store, work_id, node_id, my_epoch, str(e))
        except Exception as e2:
            logger.error("[work_session] could not fail %s after crash: %s", sid, e2)
    finally:
        with _sessions_lock:
            entry = _live_sessions.get(sid)
            if entry is not None and entry.get("thread") is threading.current_thread():
                _live_sessions.pop(sid, None)


# --------------------------------------------------------------------------- #
# Crash recovery — an ask outlives the process that made it
# --------------------------------------------------------------------------- #

def _record_and_finalize_ask_result(store, work_id: str, node_id: str, result, *, expected_epoch: int) -> None:
    """Rejoin the ordinary tool-return path after obtaining an existing ticket's result."""
    from work_objects.result_recorder import record_tool_result

    if not record_tool_result(store, work_id, node_id, result, actor="ask",
                              expected_epoch=expected_epoch, evidence_title="user response"):
        return  # A stale/ended call must not adjudicate its successor's result.

    from app.assistant.manager_runtime.execution import Owner
    node = store.load(work_id).nodes[node_id]
    ticket_id = node.payload.get("ticket_id")
    if ticket_id:
        store.settle_recovered_ticket(Owner(store, work_id, node_id, expected_epoch), ticket_id)
    _finalize_recorded_result(work_id, node_id, expected_epoch=expected_epoch)


def _finalize_recorded_result(work_id, node_id, *, expected_epoch):
    """Resume precisely the ordinary finalizer stage for an already-persisted result."""
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    from app.assistant.lib.blackboard.Blackboard import Blackboard
    from app.assistant.utils.pydantic_classes import Message
    blackboard = Blackboard()
    blackboard.update_state_value("work_node_ref", f"{work_id}::{node_id}")
    blackboard.update_state_value("dispatch_epoch", expected_epoch)
    WorkFinalizerNode(
        name="work_finalizer_node", blackboard=blackboard, agent_registry={}, tool_registry={},
    ).action_handler(Message(scope_context=room_session_scope(work_id, node_id)))
    return bool(blackboard.get_state_value("work_finalizer_result", []))


def recover_pending_finalizations() -> int:
    """Resume interrupted judgment stages; never dispatch or rerun their tools."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    store = get_dayflow_work_store()
    recovered = 0
    for summary in store.list_work_objects():
        if summary.get("status") not in {"active", "blocked"}:
            continue
        try:
            wo = store.load(summary["id"])
            for node in wo.nodes.values():
                if not wo.needs_finalization(node):
                    continue
                if node.payload.get("result_actor") != "dispatch_sweeper" and session_alive(wo.id, node.id, int(node.payload.get("dispatch_epoch") or 0)):
                    continue
                recovered += int(_finalize_recorded_result(wo.id, node.id,
                    expected_epoch=int(node.payload.get("dispatch_epoch") or 0)))
        except Exception:
            logger.error("[work_session] pending judgment recovery failed for %s", summary["id"], exc_info=True)
    return recovered


def _run_resume_ask_session(store, work_id, node_id, sid, ticket_id, timeout_s, expected_epoch):
    from app.assistant.manager_runtime.execution import Owner
    with REGISTRY.span("ticket_recovery", sid, owner=Owner(store, work_id, node_id, expected_epoch), attribution=node_id, recovery=True):
        _run_resume_ask_session_body(store, work_id, node_id, sid, ticket_id, timeout_s, expected_epoch)


def _run_resume_ask_session_body(store, work_id: str, node_id: str, sid: str,
                            ticket_id: str, timeout_s: float, expected_epoch: int) -> None:
    """Wait out the REMAINING window of a question still live after a restart.

    The user's response or expiry goes through the same recorder and finalizer as a first-time
    ask. Recovery waits on the existing ticket so the user sees the original question.
    """
    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        CreateDayflowTicketTool,
    )
    from app.assistant.ticket_manager import get_ticket_manager

    ref = f"{work_id}::{node_id}"
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is None or node.status != "dispatched" or int(node.payload.get("dispatch_epoch") or 0) != expected_epoch:
            return
        my_epoch = expected_epoch
        title = str(getattr(node, "title", "") or "")
        result = CreateDayflowTicketTool._wait_for_ticket_response(ticket_id, title, timeout_s)
        # The answer can land in the gap between the state check that sent us here and the
        # listener registering. Re-read the row before believing a timeout.
        if str((getattr(result, "data", None) or {}).get("action") or "") == "timeout":
            ticket = get_ticket_manager().get_ticket_by_id(ticket_id)
            settled = CreateDayflowTicketTool.result_for_ticket(ticket) if ticket else None
            if settled is not None:
                result = settled
        _record_and_finalize_ask_result(store, work_id, node_id, result, expected_epoch=my_epoch)
    except Exception as e:
        logger.error("[work_session] resumed ask %s crashed: %s", sid, e, exc_info=True)
    finally:
        with _sessions_lock:
            entry = _live_sessions.get(sid)
            if entry is not None and entry.get("thread") is threading.current_thread():
                _live_sessions.pop(sid, None)
        signal_work_progress(ref)


def re_arm_inflight_asks() -> int:
    """Reconnect asks whose session died with the process. Call once at boot.

    An ask is a tool call that can outlive the process running it: the thread waiting on the user
    is gone after a restart, but the QUESTION is not — it is a row in the ticket database, and it
    may already carry the answer. Without this the node sits `dispatched` with a dead session until
    the sweep fails it, and the answer the user may have given minutes earlier is discarded — then
    the architect plans the ask again and the same question goes on screen a second time.

    Driven from the TICKET side, because the ticket is the durable record and it carries the node
    ref (``trigger_context.work_node``) saying which call it belongs to. Three outcomes, one for
    each thing that can have happened while we were down:
      responded  -> record the answer and run the dispatch finalizer
      lapsed     -> record "user not reached" and run the dispatch finalizer
      still live -> wait out the remaining window, then record and finalize the result

    Returns how many asks were reconnected. Never raises: boot continues, and an ask this misses
    remains subject to the inactivity sweep.
    """
    from datetime import timedelta

    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        CreateDayflowTicketTool,
    )
    from app.assistant.ticket_manager import get_ticket_manager

    reconnected = 0
    try:
        store = get_dayflow_work_store()
        now = datetime.now(timezone.utc)
        tickets = get_ticket_manager().get_tickets(
            ticket_type="dayflow_orchestrator", limit=None)

        # Newest ticket per node wins: a superseded question is not the live one.
        newest: dict = {}
        for t in tickets:
            ctx = getattr(t, "trigger_context", None)
            ref = str(ctx.get("work_node") or "") if isinstance(ctx, dict) else ""
            if "::" not in ref:
                continue
            epoch = ctx.get("dispatch_epoch")
            if not isinstance(epoch, int):
                continue  # An unbound historical ticket cannot establish attempt ownership.
            key = (ref, epoch)
            prev = newest.get(key)
            if prev is None or (getattr(t, "created_at", None) or now) > (
                    getattr(prev, "created_at", None) or now):
                newest[key] = t

        for (ref, epoch), ticket in newest.items():
            work_id, _, node_id = ref.partition("::")
            try:
                wo = store.load(work_id)
                node = wo.nodes.get(node_id)
            except Exception:
                continue
            if node is None or not wo.is_work_unit(node) or node.status != "dispatched":
                continue                      # already ended, or never in flight
            if int(node.payload.get("dispatch_epoch") or 0) != epoch:
                continue
            if session_alive_by_id(session_id_for(work_id, node_id), epoch):
                continue                      # a live thread still owns this call

            from app.assistant.manager_runtime.execution import Owner
            if store.execution_running(Owner(store, work_id, node_id, epoch)):
                continue  # Another live process still owns the ticket wait.
            result = CreateDayflowTicketTool.result_for_ticket(ticket)
            if result is not None:
                _record_and_finalize_ask_result(
                    store, work_id, node_id, result,
                    expected_epoch=epoch)
                signal_work_progress(ref)
                reconnected += 1
                logger.info("[work_session] re-armed %s from its ticket's recorded outcome", ref)
                continue

            valid_until = getattr(ticket, "valid_until", None)
            remaining = (valid_until - now).total_seconds() if valid_until is not None else 0.0
            if remaining <= 0:
                continue                      # result_for_ticket would already have settled it
            sid = session_id_for(work_id, node_id)
            thread = threading.Thread(
                target=_run_resume_ask_session,
                args=(store, work_id, node_id, sid, ticket.ticket_id, remaining, epoch),
                name=sid, daemon=True)
            _start_registered_thread(sid, thread, epoch)
            reconnected += 1
            logger.info("[work_session] re-armed %s — its question is still live, %.0fs left",
                        ref, remaining)
    except Exception:
        logger.error("[work_session] re-arming in-flight asks failed — any missed ask ends through "
                     "the inactivity sweep instead", exc_info=True)
    if reconnected:
        logger.info("[work_session] reconnected %d in-flight ask(s) after restart", reconnected)
    return reconnected
