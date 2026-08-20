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
# An ask is a tool call whose result is the user's reply; the ticket's validity
# window IS the call's timeout. Expired unanswered -> the sweeper fails the node
# (timed-out tool call) and work_repair adjudicates. There is no re-ask timer.
_ASK_TIMEOUT_HOURS = 1


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


def _live_ask(wo, exclude_node_id: str):
    """The work object's in-flight ask, if any: a `dispatched` node whose
    wake_kind is `user_reply` — its question is out to the user right now.
    Pure id/state join — no wording."""
    for n in wo.nodes.values():
        if n.id == exclude_node_id:
            continue
        if n.status == "dispatched" and n.wake_kind == "user_reply":
            return n
    return None


def _ticket(store, work_id: str, node_id: str, node) -> None:
    """Surface the node's question as a ticket and mark the node `dispatched` — an in-flight
    tool call whose result is the user's reply. It ends exactly like any tool call: the
    reply/dismissal lands as its result (materializer -> done), or the ticket expires
    unanswered and the sweeper fails it as a timed-out call for work_repair to adjudicate.

    ONE LIVE ASK PER WORK OBJECT (2026-08-18 walk-storm): if a sibling ask is already
    in flight, this node does NOT surface a second question — it queues (deferred to
    the ask timeout; sink, not drop) and gets its turn once the live ask resolves."""
    from work_objects.model import utcnow
    wo = store.load(work_id)
    live = _live_ask(wo, exclude_node_id=node_id)
    if live is not None:
        logger.warning(
            "[node_dispatch] %s::%s wants to ask the user but %s already has its question "
            "out — queuing behind it, no second ticket", work_id, node_id, live.id)
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "waiting"},
                    actor="node_dispatch")
        store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": "time",
                                   "wake_at": utcnow() + timedelta(hours=_ASK_TIMEOUT_HOURS),
                                   "wake_ref": node.wake_ref}, actor="node_dispatch")
        return
    _surface_ticket(store, work_id, node_id, node)
    store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "dispatched"},
                actor="node_dispatch")
    store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": None, "wake_ref": node.wake_ref or node.title},
                actor="node_dispatch")
    logger.info("[node_dispatch] ticketed user %s::%s (in-flight ask; reply or timeout ends it)",
                work_id, node_id)


def _supersede_open_asks(tm, work_id: str, node_id: str) -> None:
    """One live ask ticket per work object: before a new ask surfaces, expire any
    still-open (pending/proposed) dayflow ask ticket bound to the same work_id —
    the old question disappears as the new one appears, instead of stacking in
    the UI. Joined purely on trigger_context.work_node ids — no wording."""
    prefix = f"{work_id}::"
    for old in tm.get_tickets_pending_or_proposed():
        if str(old.ticket_type or "") != "dayflow_orchestrator":
            continue
        ctx = old.trigger_context if isinstance(old.trigger_context, dict) else {}
        if not str(ctx.get("work_node") or "").startswith(prefix):
            continue
        tm.mark_expired(old.ticket_id, reason=f"superseded by {work_id}::{node_id}")
        logger.info("[node_dispatch] expired open ask %s (superseded by %s::%s)",
                    old.ticket_id, work_id, node_id)


def _research_pod_ids(wo, node_id: str) -> list[str]:
    """Research pods this delivery hands over: the pod_refs on the node itself, its
    depends_on upstreams, and those upstreams' evidence/artifact children (parent-linked
    or produces-linked). Pure id walk — the kind is read off the pod URI, no wording."""
    ups = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
    pool = {node_id, *ups}
    for uid in ups:
        produced = {e.dst for e in wo.edges if e.src == uid and e.relation == "produces"}
        pool |= {m.id for m in wo.nodes.values() if m.parent_id == uid} | produced
    ids: list[str] = []
    for nid in sorted(pool):
        n = wo.nodes.get(nid)
        ref = str(getattr(n, "pod_ref", "") or "") if n is not None else ""
        if ref.startswith("datapod:research_finding:") and ref not in ids:
            ids.append(ref)
    return ids


def _surface_ticket(store, work_id: str, node_id: str, node) -> None:
    """Phrase + surface the node's communication as a ticket tagged with this node; the reply is matched
    back via trigger_context.work_node on a later tick. Content gathering is the
    ticket_builder_manager's job: its planner reads this work object's graph (and
    dereferences pod ids) so a deliver-goal's ticket carries the produced result
    itself — dispatch hands over ids and the goal, never pre-chewed content."""
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.ticket_manager import get_ticket_manager
    # node.content is the planner's full instruction — the material the user
    # actually needs — and must be the primary source. wake_ref is a wake-match
    # primitive (often just the node title); letting it shadow content is how
    # "planner + pencil + instrument on Aug 21" reached the user as "check on
    # the supplies needed for music class".
    want = str(node.content or "").strip() or str(getattr(node, "wake_ref", "") or "").strip()
    brief = (f"Communicate with the user so a task can proceed. Task: {node.title}. "
             f"What I need from them: {want}. "
             f"Phrase it as a warm, direct message addressed to them; "
             f"keep every concrete detail (who, what, when) — do not generalize them away.")
    formatted = CreateDayflowTicketTool._format_brief(brief, work_node_ref=f"{work_id}::{node_id}")
    title = (str(formatted.get("title") or node.title or "I need your input").strip())[:80]
    message = str(formatted.get("message") or want or title).strip()
    # Full-report page links ride DETERMINISTICALLY after the composed message — a pod id
    # must reach the user exactly or not at all, never via LLM transcription. The popup
    # linkifies /research/... paths into anchors.
    report_pods = _research_pod_ids(store.load(work_id), node_id)
    if report_pods:
        links = "\n".join(f"Full report: /research/{p}" for p in report_pods)
        message = f"{message}\n\n{links}"
    tm = get_ticket_manager()
    _supersede_open_asks(tm, work_id, node_id)
    ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_TICKET_SUGGESTION_TYPE,
                              title=title, message=message, trigger_context={"work_node": f"{work_id}::{node_id}"},
                              valid_hours=_ASK_TIMEOUT_HOURS)
    if not ticket or not tm.mark_proposed(ticket.ticket_id):
        return
    payload = ticket.to_dict()
    payload["button_layout"] = "decision"
    payload["plan_mode_available"] = True
    DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
