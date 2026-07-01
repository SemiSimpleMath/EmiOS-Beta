"""Route + dispatch ONE work-object node — the shared core used by BOTH the pipeline's
work_node_dispatch_node (per tick, delegate_to already decided by the switchboard) AND the scheduler's
precise time-wake fire (route_and_dispatch, which asks the switchboard itself).

A work node is anything DISPATCHABLE; the switchboard decides where by READING the node, not by a type:
  - create_dayflow_ticket -> surface a ticket to the user and await their response (which becomes the result).
  - anything else (run_work_node) -> run the node via the worker (work_emi_team).

Both are just tool calls that produce a RESULT recorded on the graph; the finalizer judges the result.
There is no one-way ticket — every ticket awaits a response. This module is that dispatch; it does not judge.
"""
from datetime import timedelta

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_TICKET_SUGGESTION_TYPE = "work_notify"
_REASK_HOURS = 1


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
    from work_objects.work_runtime import work_on
    status = work_on(store, work_id, node_id=node_id)   # context + worker + result, all on the graph
    logger.info("[node_dispatch] work %s::%s -> %s", work_id, node_id, status)


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
