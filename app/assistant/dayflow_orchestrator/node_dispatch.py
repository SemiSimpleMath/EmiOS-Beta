"""Route + dispatch ONE work-object node — the shared core used by BOTH the pipeline's
work_node_dispatch_node (per tick, delegate_to already decided by the switchboard) AND the scheduler's
precise time-wake fire (route_and_dispatch, which asks the switchboard itself).

A work node is anything DISPATCHABLE; the switchboard decides where by READING the node, not by a type:
  - create_dayflow_ticket -> surface a ticket to the user and await their response (which becomes the result).
  - anything else (run_work_node) -> run the node via the worker (work_emi_team).

Both are just tool calls that produce a RESULT recorded on the graph; the finalizer judges the result.
There is no one-way ticket — every ticket awaits a response. This module is that dispatch; it does not judge.

Execution model: ONE THREAD PER OPEN TASK. Worker dispatch claims the node (-> dispatched) and runs the
worker on its own job thread; the dispatching tick/wake returns immediately. The GRAPH is the return
channel (the result lands as evidence + status), the work-progress signal brings the next planning pass,
and dispatch_sweeper.sweep_stuck_work_nodes supervises the in-flight jobs (orphaned/frozen -> failed ->
work_repair adjudicates). The in-flight registry below is that supervisor's job table.
"""
import threading
from datetime import datetime, timedelta, timezone

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_TICKET_SUGGESTION_TYPE = "work_notify"
_REASK_HOURS = 1

# In-flight job registry — one entry per running work-node job thread. The registry entry is created
# BEFORE the node is claimed, so a `dispatched` node with no entry is definitively orphaned (restart /
# thread crash), not racing its own registration.
_JOB_GRACE_SECONDS = 60
_inflight_lock = threading.Lock()
_inflight: dict = {}   # "work_id::node_id" -> {"thread": Thread, "started_at": datetime}


def job_alive(ref: str) -> bool:
    """True if a live job thread owns this node (or one was registered within the grace window)."""
    with _inflight_lock:
        entry = _inflight.get(ref)
    if entry is None:
        return False
    if entry["thread"].is_alive():
        return True
    age = (datetime.now(timezone.utc) - entry["started_at"]).total_seconds()
    return age < _JOB_GRACE_SECONDS


def job_started_at(ref: str):
    """The job's start time, or None when no job is registered."""
    with _inflight_lock:
        entry = _inflight.get(ref)
    return entry["started_at"] if entry else None


def _unregister_job(ref: str) -> None:
    """Remove this thread's registry entry — OWNER-CHECKED. A zombie thread
    finishing after the sweeper failed its incarnation and repair
    re-dispatched the node must not delete the SUCCESSOR job's entry
    (audit W2: the sweeper would then orphan-fail the live successor)."""
    with _inflight_lock:
        entry = _inflight.get(ref)
        if entry is not None and entry.get("thread") is threading.current_thread():
            _inflight.pop(ref, None)


def dispatch_node(store, work_id: str, node_id: str, delegate_to: str) -> None:
    """Carry out the switchboard's routing decision for one node. The switchboard read the node's GOAL and
    picked the tool:
      - create_dayflow_ticket -> surface the ticket to the user + await their response (becomes the result)
      - run_work_node (anything else) -> the worker does it.
    Both produce a result on the graph; the finalizer judges it. There is no one-way ticket."""
    if str(delegate_to or "").strip() == "create_dayflow_ticket":
        node = store.load(work_id).nodes.get(node_id)
        if node is None:
            return
        _ticket(store, work_id, node_id, node)
    else:
        _do_work(store, work_id, node_id)


def route_and_dispatch(store, work_id: str, node_id: str, scope=None) -> None:
    """A ready node needs routing OUTSIDE the planning tick (a precise time-wake fired). Ask the switchboard
    — the SAME router the pipeline uses — by reading the node's content, then dispatch. This replaces the
    scheduler firing the node straight into the worker, so a timed ticket is surfaced to the user and timed
    work goes to the worker, both decided by reading the node."""
    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        return
    task = (f"{node.title}. {node.content}" if node.content else (node.title or "")).strip()
    if scope is None:
        from app.assistant.scope.loader import load_scope_for_source
        scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="node_dispatch")
    try:
        agent = DI.agent_factory.create_agent("dayflow_orchestrator::switchboard")
        result = agent.action_handler(Message(task=task, scope_context=scope))
        data = getattr(result, "data", {}) or {}
        delegate_to = str(data.get("delegate_to", "") or "").strip()
    except Exception as e:
        logger.error("[node_dispatch] switchboard routing failed for %s::%s: %s — defaulting to work",
                     work_id, node_id, e)
        delegate_to = ""
    logger.info("[node_dispatch] routed %s::%s -> %s", work_id, node_id, delegate_to or "run_work_node")
    dispatch_node(store, work_id, node_id, delegate_to)


def _do_work(store, work_id: str, node_id: str) -> None:
    """Claim the node and run the worker on its OWN job thread. The caller (tick loop or time wake)
    returns immediately — results land on the graph, the finalizer judges them on a later tick, and
    the supervisor sweep handles a job that dies or freezes."""
    ref = f"{work_id}::{node_id}"
    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        return
    # Claimable statuses only. A node already `dispatched` has a live (or
    # sweeper-supervised) incarnation — starting a second thread would race
    # it; a finished node has nothing to run. Refuse loudly.
    if node.status not in {"proposed", "waiting", "actionable"}:
        logger.error(
            "[node_dispatch] refusing dispatch of %s — status %r is not claimable",
            ref, node.status,
        )
        return
    thread = threading.Thread(target=_run_job, args=(store, work_id, node_id),
                              name=f"work-node::{ref}", daemon=True)
    with _inflight_lock:
        _inflight[ref] = {"thread": thread, "started_at": datetime.now(timezone.utc)}
    # Claim synchronously (registry entry first — see registry note above) so no later planning pass
    # can pick this node again; run_node's own pickup-flip then no-ops.
    store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "dispatched"},
                actor="node_dispatch")
    thread.start()
    logger.info("[node_dispatch] job thread started for %s", ref)


def _run_job(store, work_id: str, node_id: str) -> None:
    """The job thread: run the worker, then hand the baton to the next planning pass. Mechanical
    post-processing (result -> evidence, done/failed) is run_node's tail, on this same thread."""
    ref = f"{work_id}::{node_id}"
    # My incarnation: the claim (before thread start) bumped the epoch. The
    # crash-handler write below carries it so a zombie crash can't fail a
    # successor's incarnation.
    my_epoch = None
    try:
        node = store.load(work_id).nodes.get(node_id)
        if node is not None:
            my_epoch = int(node.payload.get("dispatch_epoch") or 0)
        from work_objects.discharge import discharge_node
        from work_objects.scope import orchestrator_scope
        # INTERIM (dies with the WorkSession step): scope made explicit at the call site;
        # the session layer will pass the dayflow ROOM's derived scope instead.
        discharge_node(store, work_id, node_id,
                       scope_context=orchestrator_scope(work_id=work_id))
        n = store.load(work_id).nodes.get(node_id)
        logger.info("[node_dispatch] work %s -> %s", ref, n.status if n else "missing")
    except Exception as e:
        logger.error("[node_dispatch] job thread for %s crashed: %s", ref, e, exc_info=True)
        try:
            fail_data = {"work_id": work_id, "node_id": node_id, "status": "failed"}
            if my_epoch is not None:
                fail_data["expected_dispatch_epoch"] = my_epoch
            store.apply("set_status", fail_data, actor="node_dispatch")
        except Exception as e2:
            logger.error("[node_dispatch] could not fail %s after job crash: %s", ref, e2)
    finally:
        _unregister_job(ref)
        signal_work_progress(ref)


def signal_work_progress(ref: str) -> None:
    """A node just reached a result (done/failed) or a reply landed — nudge the scheduler for a prompt
    follow-up tick so the finalizer/repair judge it and dependents advance within minutes, instead of at
    the next ceiling tick. Latency-only: a lost signal just means the next scheduled tick picks it up."""
    try:
        DI.event_hub.publish(Message(event_topic="dayflow_work_progress", content=ref))
    except Exception as e:
        logger.warning("[node_dispatch] work-progress signal failed for %s: %s", ref, e)


def _ticket(store, work_id: str, node_id: str, node) -> None:
    """Surface the node as a ticket to the user + park it OUT of the ready set to `waiting` (in-flight,
    awaiting their response, which becomes the node's result — the materializer records the reply on a
    later tick). Until they respond it re-surfaces on the re-ask timer. There is no one-way ticket."""
    from work_objects.model import utcnow
    _surface_ticket(work_id, node_id, node)
    store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "waiting"}, actor="node_dispatch")
    store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": utcnow() + timedelta(hours=_REASK_HOURS),
                               "wake_ref": node.wake_ref or node.title}, actor="node_dispatch")
    logger.info("[node_dispatch] ticketed user %s::%s (parked until reply/re-ask)", work_id, node_id)


def _surface_ticket(work_id: str, node_id: str, node) -> None:
    """Phrase + surface the node's communication as a ticket tagged with this node; the reply is matched
    back via trigger_context.work_node on a later tick."""
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.ticket_manager import get_ticket_manager
    want = str(getattr(node, "wake_ref", "") or "").strip() or str(node.content or "").strip()
    brief = (f"Communicate with the user so a task can proceed. Task: {node.title}. "
             f"What I need from them: {want}. Phrase it as a warm, direct message addressed to them.")
    formatted = CreateDayflowTicketTool._format_brief(brief)
    title = (str(formatted.get("title") or node.title or "I need your input").strip())[:80]
    message = str(formatted.get("message") or want or title).strip()
    tm = get_ticket_manager()
    ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_TICKET_SUGGESTION_TYPE,
                              title=title, message=message, trigger_context={"work_node": f"{work_id}::{node_id}"},
                              valid_hours=_REASK_HOURS)
    if not ticket or not tm.mark_proposed(ticket.ticket_id):
        return
    payload = ticket.to_dict()
    payload["button_layout"] = "decision"
    payload["plan_mode_available"] = True
    DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
