"""Route + dispatch ONE work-object node — the shared core used by BOTH the pipeline's
work_node_dispatch_node (per tick, delegate_to already decided by the switchboard) AND the scheduler's
precise time-wake fire (a targeted room invocation routed by tick_router_node).

A work node is anything DISPATCHABLE; the switchboard decides where by READING the node, not by a type:
  - create_dayflow_ticket -> surface a ticket to the user and await their response (which becomes the result).
  - anything else (run_work_node) -> run the node via the worker (work_emi_team).

Both are just tool calls that produce a RESULT recorded on the graph; the finalizer judges the result.
There is no one-way ticket — every ticket awaits a response. This module is that dispatch; it does not judge.

Execution model: ONE THREAD PER OPEN TASK. Worker dispatch claims the node (-> dispatched) and runs the
worker on its own job thread; the dispatching tick/wake returns immediately. The GRAPH is the return
channel (the result lands as evidence + status), the work-progress signal brings the next planning pass,
and dispatch_sweeper.sweep_stuck_work_nodes supervises the in-flight jobs (orphaned/frozen -> failed ->
work_repair adjudicates). The session registry (work_session.py) is that supervisor's job table.
"""
import threading
from datetime import datetime, timedelta, timezone

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_TICKET_SUGGESTION_TYPE = "work_notify"
_REASK_HOURS = 1


def dispatch_node(store, work_id: str, node_id: str, delegate_to: str) -> None:
    """Carry out the switchboard's routing decision for one node. ONE dispatch path:
    the WorkSession (a copy of the orchestrator room, open until its call returns) hosts
    both branches — ticket (surface + park) and work (room-scoped worker thread)."""
    from app.assistant.dayflow_orchestrator.work_session import open_session
    open_session(store, work_id, node_id, delegate_to)


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
