"""WorkSession — a copy of the orchestrator room, open until its call returns.

The single dispatch host for dayflow work nodes (work-session rewrite,
2026-08-04). Every trigger (planning tick, scheduler time-wake) converges here:

  open_session(store, work_id, node_id, delegate_to)

Both branches are the same shape — a session thread that makes ONE tool call and
records its result:

- ``create_dayflow_ticket`` -> ask the user; the tool blocks until they answer,
  close it, or its window lapses, and returns a ToolResult either way.
- anything else -> run the node through the worker via ``discharge_node`` under
  the DAYFLOW ROOM'S scope.

The dispatching tick returns immediately in both cases; the GRAPH is the return
channel. The ticket branch used to be the exception — it surfaced a ticket by
hand, parked the node on a ``user_reply`` wake and returned nothing, so the
user's answer had to be reconstructed later from the ticket store.

Authority is DERIVED, never minted: the scope is the room's own ``scope.yaml``
loaded through the standard loader (pods ``[all]``, authority 95, KG writes —
exactly what every other consumer of the room gets), so a worker sees precisely
what the orchestrator sees. The 2026-08-03 forward-email flounder — a worker
blind to an email pod its goal referenced — is structurally impossible here.

The session registry below is the in-flight liveness table the supervisor
reads (``session_alive``); the graph carries the durable join
(``payload.session_id``). Threads die with the process; the graph doesn't —
after a restart no session is alive, the sweeper fails the orphans, and
work_repair re-issues them.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_ROOM_ID = "dayflow_orchestrator"
_SESSION_GRACE_SECONDS = 60

_sessions_lock = threading.Lock()
_live_sessions: dict = {}   # session_id -> {"thread": Thread, "started_at": datetime}


def session_id_for(work_id: str, node_id: str) -> str:
    return f"work_session::{work_id}::{node_id}"


def session_alive_by_id(sid: str) -> bool:
    """True if the named session has a live thread (or registered within the grace window)."""
    with _sessions_lock:
        entry = _live_sessions.get(sid)
    if entry is None:
        return False
    if entry["thread"].is_alive():
        return True
    age = (datetime.now(timezone.utc) - entry["started_at"]).total_seconds()
    return age < _SESSION_GRACE_SECONDS


def session_alive(work_id: str, node_id: str) -> bool:
    """True if a live session owns this node directly (its own session id)."""
    return session_alive_by_id(session_id_for(work_id, node_id))


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


def open_session(store, work_id: str, node_id: str, delegate_to: str) -> None:
    """Carry out the switchboard's routing decision for one node — the ONE dispatch path.

    One shape for both: registry entry FIRST (so a `dispatched` node with no session is
    definitively orphaned, never racing its own registration), then the session thread makes
    its one tool call. The dispatching pass returns immediately either way."""
    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        return

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

    sid = session_id_for(work_id, node_id)
    thread = threading.Thread(target=_run_dispatch_room,
                              args=(store, work_id, node_id, sid, delegate_to),
                              name=sid, daemon=True)
    with _sessions_lock:
        _live_sessions[sid] = {"thread": thread, "started_at": datetime.now(timezone.utc)}
    # Ownership is a graph fact the supervisor reads. The gate already claimed the node; this
    # stamps WHICH session owns it, registry-first so a `dispatched` node with no session is
    # definitively orphaned rather than racing its own registration.
    store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                               "status": node.status, "session_id": sid},
                actor="work_session")
    thread.start()
    logger.info("[work_session] session %s started", sid)


def _run_dispatch_room(store, work_id: str, node_id: str, sid: str, delegate_to: str) -> None:
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
    my_epoch = None
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is None:
            return
        my_epoch = int(node.payload.get("dispatch_epoch") or 0)

        manager = DI.multi_agent_manager_factory.create_manager("dayflow_dispatch_manager")
        # The room's whole input. The arguments node reads the NODE for everything else, so
        # these two values plus the graph are the entire contract.
        manager.blackboard.update_state_value("delegate_to", str(delegate_to).strip())
        manager.blackboard.update_state_value("work_node_ref", ref)
        DI.manager_invoker.invoke(manager, Message(
            event_topic="dayflow_dispatch",
            sender="system",
            receiver=None,
            task="",
            information="",
            content="",
            data={"work_node": ref, "delegate_to": str(delegate_to).strip()},
            scope_context=room_session_scope(work_id, node_id),
        ))
        n = store.load(work_id).nodes.get(node_id)
        logger.info("[work_session] %s -> %s", sid, n.status if n else "missing")
    except Exception as e:
        logger.error("[work_session] session %s crashed: %s", sid, e, exc_info=True)
        try:
            fail_data = {"work_id": work_id, "node_id": node_id, "status": "failed"}
            if my_epoch is not None:
                fail_data["expected_dispatch_epoch"] = my_epoch
            store.apply("set_status", fail_data, actor="work_session")
        except Exception as e2:
            logger.error("[work_session] could not fail %s after crash: %s", sid, e2)
    finally:
        with _sessions_lock:
            entry = _live_sessions.get(sid)
            if entry is not None and entry.get("thread") is threading.current_thread():
                _live_sessions.pop(sid, None)
        signal_work_progress(ref)


# --------------------------------------------------------------------------- #
# Crash recovery — an ask outlives the process that made it
# --------------------------------------------------------------------------- #

def _run_resume_ask_session(store, work_id: str, node_id: str, sid: str,
                            ticket_id: str, timeout_s: float) -> None:
    """Wait out the REMAINING window of a question still live after a restart.

    Same ending as a first-time ask — the user's response, or the window lapsing — recorded by the
    same recorder. It does NOT create a ticket: the question is already on screen, and minting a
    second one would ask the user twice for something they can still see.
    """
    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        CreateDayflowTicketTool,
    )
    from app.assistant.ticket_manager import get_ticket_manager
    from work_objects.result_recorder import record_tool_result

    ref = f"{work_id}::{node_id}"
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is None:
            return
        my_epoch = int(node.payload.get("dispatch_epoch") or 0)
        title = str(getattr(node, "title", "") or "")
        result = CreateDayflowTicketTool._wait_for_ticket_response(ticket_id, title, timeout_s)
        # The answer can land in the gap between the state check that sent us here and the
        # listener registering. Re-read the row before believing a timeout.
        if str((getattr(result, "data", None) or {}).get("action") or "") == "timeout":
            ticket = get_ticket_manager().get_ticket_by_id(ticket_id)
            settled = CreateDayflowTicketTool.result_for_ticket(ticket) if ticket else None
            if settled is not None:
                result = settled
        record_tool_result(store, work_id, node_id, result, actor="ask",
                           expected_epoch=my_epoch, evidence_title="user response")
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
    may already carry the answer. Without this the node sits `dispatched` with a dead session, the
    orphan sweep fails it, and repair re-asks — discarding an answer the user may have given
    minutes earlier and putting the same question on screen a second time.

    Driven from the TICKET side, because the ticket is the durable record and it carries the node
    ref (``trigger_context.work_node``) saying which call it belongs to. Three outcomes, one for
    each thing that can have happened while we were down:
      responded  -> land the answer as the node's result
      lapsed     -> land "user not reached"
      still live -> wait out what remains of its window, on the ticket already on screen

    Returns how many asks were reconnected. Never raises: boot continues, and an ask this misses
    still ends through the orphan sweep.
    """
    from datetime import timedelta

    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        CreateDayflowTicketTool,
    )
    from app.assistant.ticket_manager import get_ticket_manager
    from work_objects.result_recorder import record_tool_result

    reconnected = 0
    try:
        store = get_dayflow_work_store()
        now = datetime.now(timezone.utc)
        tickets = get_ticket_manager().get_tickets(
            ticket_type="dayflow_orchestrator", suggestion_type="work_notify",
            since_utc=now - timedelta(hours=48), limit=200)

        # Newest ticket per node wins: a superseded question is not the live one.
        newest: dict = {}
        for t in tickets:
            ctx = getattr(t, "trigger_context", None)
            ref = str(ctx.get("work_node") or "") if isinstance(ctx, dict) else ""
            if "::" not in ref:
                continue
            prev = newest.get(ref)
            if prev is None or (getattr(t, "created_at", None) or now) > (
                    getattr(prev, "created_at", None) or now):
                newest[ref] = t

        for ref, ticket in newest.items():
            work_id, _, node_id = ref.partition("::")
            try:
                node = store.load(work_id).nodes.get(node_id)
            except Exception:
                continue
            if node is None or node.status != "dispatched":
                continue                      # already ended, or never in flight
            if session_alive_by_id(session_id_for(work_id, node_id)):
                continue                      # a live thread still owns this call

            result = CreateDayflowTicketTool.result_for_ticket(ticket)
            if result is not None:
                record_tool_result(store, work_id, node_id, result, actor="ask",
                                   expected_epoch=int(node.payload.get("dispatch_epoch") or 0),
                                   evidence_title="user response")
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
                args=(store, work_id, node_id, sid, ticket.ticket_id, remaining),
                name=sid, daemon=True)
            with _sessions_lock:
                _live_sessions[sid] = {"thread": thread, "started_at": now}
            thread.start()
            reconnected += 1
            logger.info("[work_session] re-armed %s — its question is still live, %.0fs left",
                        ref, remaining)
    except Exception:
        logger.error("[work_session] re-arming in-flight asks failed — any missed ask ends through "
                     "the orphan sweep instead", exc_info=True)
    if reconnected:
        logger.info("[work_session] reconnected %d in-flight ask(s) after restart", reconnected)
    return reconnected
