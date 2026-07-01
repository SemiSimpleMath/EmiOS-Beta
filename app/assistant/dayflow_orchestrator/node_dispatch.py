"""Route + dispatch ONE work-object node — the shared core used by BOTH the pipeline's
work_node_dispatch_node (per tick, delegate_to already decided by the switchboard) AND the scheduler's
precise time-wake fire (route_and_dispatch, which asks the switchboard itself).

A work node is anything DISPATCHABLE; the switchboard decides where by READING the node, not by a type:
  - create_dayflow_ticket -> communicate with the user (one-way notify, or an ask that awaits a reply).
  - anything else (run_work_node) -> run the node via the worker (work_emi_team).

Both a notify and a manager call are just tool calls that produce a RESULT recorded on the graph; the
finalizer judges the result. This module is that dispatch; it does not judge.
"""
from datetime import timedelta

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_NOTIFY_SUGGESTION_TYPE = "work_notify"
_REASK_HOURS = 1


def dispatch_node(store, work_id: str, node_id: str, delegate_to: str) -> None:
    """Carry out the switchboard's routing decision for one node."""
    if str(delegate_to or "").strip() == "create_dayflow_ticket":
        _communicate(store, work_id, node_id)
    else:
        _do_work(store, work_id, node_id)


def route_and_dispatch(store, work_id: str, node_id: str, scope=None) -> None:
    """A ready node needs routing OUTSIDE the planning tick (a precise time-wake fired). Ask the switchboard
    — the SAME router the pipeline uses — by reading the node's content, then dispatch. This replaces the
    scheduler firing the node straight into the worker, so a timed notification is delivered as a
    notification and timed work goes to the worker, both decided by reading the node."""
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
        delegate_to = str((getattr(result, "data", {}) or {}).get("delegate_to", "") or "").strip()
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


def _communicate(store, work_id: str, node_id: str) -> None:
    from work_objects.store import FAMILY_BY_TYPE
    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        return
    wake_kind = str(getattr(node, "wake_kind", None) or "")
    family = FAMILY_BY_TYPE.get(getattr(node, "type", "") or "", "spine")

    # ONE-WAY NOTIFY — a notify-family node with no reply wake: deliver it. The delivered notification IS the
    # tool result — record it as evidence + complete the node (done), symmetric with a manager call; the
    # finalizer then closes it. A spine WORK node the switchboard sent to the ticket side is NOT a completed
    # notification, so it falls through to the ASK path.
    if family == "notify" and wake_kind != "user_reply":
        from app.assistant.lib.tools.create_work_notification.create_work_notification import CreateWorkNotificationTool
        if CreateWorkNotificationTool._create(work_id, node_id) is not None:
            from work_objects.model import new_id
            store.apply("add_node", {"work_id": work_id, "id": new_id("result"), "type": "evidence",
                                     "parent_id": node_id, "status": "assumed", "created_by": "node_dispatch",
                                     "title": "notification delivered", "content": (node.content or "")},
                        actor="node_dispatch")
            store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "done"}, actor="node_dispatch")
            logger.info("[node_dispatch] notified %s::%s -> done (finalizer closes)", work_id, node_id)
        else:
            logger.warning("[node_dispatch] notify produced no ticket for %s::%s; retry next tick", work_id, node_id)
        return

    # ASK — a user_reply ask, OR a node the switchboard judged needs the user. Surface it and park OUT of the
    # ready set to `waiting` (in-flight, awaiting the reply). The materializer records the reply as the node's
    # result on a later tick.
    _surface_ask(work_id, node_id, node)
    from work_objects.model import utcnow
    store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "waiting"}, actor="node_dispatch")
    store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": utcnow() + timedelta(hours=_REASK_HOURS),
                               "wake_ref": node.wake_ref or node.title}, actor="node_dispatch")
    logger.info("[node_dispatch] asked user %s::%s (parked until reply/re-ask)", work_id, node_id)


def _surface_ask(work_id: str, node_id: str, node) -> None:
    """Phrase + surface the node's question as a decision ticket tagged with this node; the reply is matched
    back via trigger_context.work_node on a later tick."""
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.ticket_manager import get_ticket_manager
    want = str(getattr(node, "wake_ref", "") or "").strip() or str(node.content or "").strip()
    brief = (f"Ask the user a question so a task can proceed. Task: {node.title}. "
             f"What I need from them: {want}. Phrase it as a warm, direct question addressed to them.")
    formatted = CreateDayflowTicketTool._format_brief(brief)
    title = (str(formatted.get("title") or node.title or "I need your input").strip())[:80]
    message = str(formatted.get("message") or want or title).strip()
    tm = get_ticket_manager()
    ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_NOTIFY_SUGGESTION_TYPE,
                              title=title, message=message, trigger_context={"work_node": f"{work_id}::{node_id}"},
                              valid_hours=_REASK_HOURS)
    if not ticket or not tm.mark_proposed(ticket.ticket_id):
        return
    payload = ticket.to_dict()
    payload["button_layout"] = "decision"
    payload["plan_mode_available"] = True
    DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
