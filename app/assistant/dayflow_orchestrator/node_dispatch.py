"""Route + dispatch ONE work-object node — the shared core used by BOTH the pipeline's
work_node_dispatch_node (per tick, delegate_to already decided by the switchboard) AND the scheduler's
precise time-wake fire (a wake pass in dayflow_wake_manager).

A work node is anything DISPATCHABLE; the switchboard decides where by READING the node, not by a type:
  - create_dayflow_ticket -> surface a ticket to the user and await their response (which becomes the result).
  - anything else (work_emi_team_manager) -> run the node via the worker (work_emi_team).

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
# An ask is a tool call whose result is the user's response; the ticket's validity
# window IS the call's timeout. Expired unanswered is a result too ("notify expired,
# user not reached") and the finalizer judges it. There is no re-ask timer — asking
# again is a planning decision.
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


def surface_and_await(store, work_id: str, node_id: str, node):
    """Ask the user, as a TOOL CALL, and return its ToolResult.

    `create_dayflow_ticket` surfaces the question and blocks until the user answers it, closes it,
    or the ticket's validity window lapses — returning a real ToolResult either way (the response,
    or ``action="timeout"``). That is a tool call in every sense, so this makes it one: the node is
    already claimed by the dispatch gate, the call runs on the node's own session thread, and the
    result goes to the same recorder a manager's result does.

    It used to be special. This function minted the ticket by hand — borrowing one formatting helper
    off the tool it declined to call — parked the node with a ``user_reply`` wake, and returned
    nothing, so the result had to be reconstructed afterwards from the ticket store by a scan in the
    materializer. A reply that ANSWERED a question survived that; a reply that KILLED the objective
    did not, because the steward read it first and abandoned the work object, and the scan skips
    terminal objects. Of 114 work objects the steward dropped between 2026-09-01 and 09-16, five
    carried a recorded reply.
    """
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        CreateDayflowTicketTool,
    )
    from app.assistant.utils.pydantic_classes import ToolMessage

    # node.content is the planner's full instruction — the material the user actually needs — and
    # must be the primary source. wake_ref is a wake-match primitive (often just the node title);
    # letting it shadow content is how "planner + pencil + instrument on Aug 21" reached the user as
    # "check on the supplies needed for music class".
    want = str(node.content or "").strip() or str(getattr(node, "wake_ref", "") or "").strip()
    report_pods = _research_pod_ids(store.load(work_id), node_id)
    brief = (f"Communicate with the user so a task can proceed. Task: {node.title}. "
             f"What I need from them: {want}. "
             f"Phrase it as a warm, direct message addressed to them; "
             f"keep every concrete detail (who, what, when) — do not generalize them away.")
    if report_pods:
        # The composer writes SHORT when a page carries the body (owner ask, 2026-08-22:
        # "short question and link to the analysis").
        brief += (" A full-report page link will be attached below your message "
                  "automatically — keep the message brief: the decision or question "
                  "and the few points the user needs at a glance; the linked page "
                  "carries the full analysis.")

    tool_message = ToolMessage(
        tool_name="create_dayflow_ticket",
        tool_data={"arguments": {
            "ticket_brief": brief,
            # Full-report page links ride DETERMINISTICALLY after the composed message — a pod id
            # must reach the user exactly or not at all, never via LLM transcription.
            "append_links": [f"Full report: /research/{p}" for p in report_pods],
            "trigger_context": {"work_node": f"{work_id}::{node_id}"},
            "valid_hours": _ASK_TIMEOUT_HOURS,
            "wait_timeout_seconds": _ASK_TIMEOUT_HOURS * 3600,
        }},
    )
    logger.info("[node_dispatch] asking the user %s::%s (in-flight tool call)", work_id, node_id)
    return CreateDayflowTicketTool().execute(tool_message)


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
