"""WorkSession — a copy of the orchestrator room, open until its call returns.

The single dispatch host for dayflow work nodes (work-session rewrite,
2026-08-04). Every trigger (planning tick, scheduler time-wake) converges here:

  open_session(store, work_id, node_id, delegate_to)

- ``create_dayflow_ticket`` -> the ticket path (surface + park ``waiting`` with
  a ``user_reply`` wake) — returns immediately; the reply becomes the result.
- anything else -> claim the node (stamping ``payload.session_id`` — ownership
  is a graph fact), then run the worker on the session's own thread via
  ``discharge_node`` under the DAYFLOW ROOM'S scope.

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

    Ticket -> surface + park (returns immediately; the materializer records the reply).
    Work -> claim synchronously (registry entry FIRST, so a `dispatched` node with no
    session is definitively orphaned, never racing its own registration), then run the
    worker on the session's thread. The dispatching pass returns immediately either way."""
    from app.assistant.dayflow_orchestrator import node_dispatch as nd

    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        return
    if str(delegate_to or "").strip() == "create_dayflow_ticket":
        nd._ticket(store, work_id, node_id, node)
        return

    if node.status not in {"proposed", "waiting", "actionable"}:
        logger.error(
            "[work_session] refusing dispatch of %s::%s — status %r is not claimable",
            work_id, node_id, node.status,
        )
        return

    sid = session_id_for(work_id, node_id)
    thread = threading.Thread(target=_run_session, args=(store, work_id, node_id, sid),
                              name=sid, daemon=True)
    with _sessions_lock:
        _live_sessions[sid] = {"thread": thread, "started_at": datetime.now(timezone.utc)}
    store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                               "status": "dispatched", "session_id": sid},
                actor="work_session")
    thread.start()
    logger.info("[work_session] session %s started", sid)


def _run_session(store, work_id: str, node_id: str, sid: str) -> None:
    """The session thread: room scope -> worker -> result on the graph -> follow-up signal."""
    from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
    from work_objects.discharge import discharge_node

    ref = f"{work_id}::{node_id}"
    my_epoch = None
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is not None:
            my_epoch = int(node.payload.get("dispatch_epoch") or 0)
        result_scope = room_session_scope(work_id, node_id)
        discharge_node(store, work_id, node_id, scope_context=result_scope, session_id=sid)
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
