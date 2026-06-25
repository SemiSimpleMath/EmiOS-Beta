"""create_work_notification — one-way owner notification for a work-object `notify` node.

The work-object-aware sibling of create_dayflow_ticket: given (work_id, node_id) it loads the work
object, pulls the notify node's intent PLUS its upstream results off the graph (so "find a movie, then
notify me" actually conveys the movie), phrases a warm heads-up via the SAME ticket_builder the regular
ticket tool uses, and surfaces a NOTIFY ticket. Unlike create_dayflow_ticket it is fire-and-forget — it
never blocks waiting for a reply (a notify expects none). work_execution's PASS 1.5 calls `_create`
directly; agents can call it as a tool.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.ticket_manager import get_ticket_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult

logger = get_logger(__name__)

_SUGGESTION_TYPE = "work_oneway_notify"
_VALID_HOURS = 1


class CreateWorkNotificationTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("create_work_notification")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = CreateDayflowTicketTool._extract_arguments(tool_message)
            work_id = str(args.get("work_id") or "").strip()
            node_id = str(args.get("node_id") or "").strip()
            if not work_id or not node_id:
                raise ValueError("work_id and node_id are required")
            ticket = self._create(work_id, node_id)
            if ticket is None:
                return ToolResult(result_type="error", content="notification not created (node missing or ticket failed)", data={})
            return ToolResult(result_type="success",
                              content=f"Notified the user (ticket {ticket.ticket_id}).",
                              data={"ticket_id": ticket.ticket_id})
        except Exception as e:
            logger.error("create_work_notification failed: %s", e)
            logger.debug("create_work_notification exception", exc_info=True)
            return ToolResult(result_type="error", content=f"create_work_notification failed: {e}", data={})

    @staticmethod
    def _create(work_id: str, node_id: str):
        """Build + surface the one-way notify ticket for a work node. Returns the ticket (or None).
        Fire-and-forget: does NOT wait for a reply."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        wo = get_dayflow_work_store().load(work_id)
        node = wo.nodes.get(node_id)
        if node is None:
            return None
        intent = (node.content or node.title or "").strip()
        context = CreateWorkNotificationTool._gather_upstream_results(wo, node_id)
        brief = (
            f"Tell the user a one-way heads-up (NOT a question): {intent}."
            + (f"\nThe result/context to convey to them: {context}" if context else "")
            + " Phrase it as a warm, direct notification addressed to them."
        )
        formatted = CreateDayflowTicketTool._format_brief(brief)   # ticket_builder agent (the phrasing LLM)
        title = (str(formatted.get("title") or node.title or "Heads up").strip())[:80]
        message = str(formatted.get("message") or intent or title).strip()
        tm = get_ticket_manager()
        ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_SUGGESTION_TYPE,
                                  title=title, message=message,
                                  trigger_context={"work_node": f"{work_id}::{node_id}"},
                                  valid_hours=_VALID_HOURS)
        if not ticket or not tm.mark_proposed(ticket.ticket_id):
            return None
        payload = ticket.to_dict()
        payload["button_layout"] = "notify"     # one-way; no reply (create_dayflow_ticket's notify kind)
        DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
        return ticket

    @staticmethod
    def _gather_upstream_results(wo, node_id: str) -> str:
        """Results of the notify node's depends_on nodes — their content/findings and any produced
        evidence — so the heads-up carries what the work actually produced, not just the bare intent."""
        dep_ids = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
        out = []
        for did in dep_ids:
            d = wo.nodes.get(did)
            if d is None:
                continue
            r = (d.content or d.pod_ref or "").strip()
            if r:
                out.append(f"{d.title}: {r}" if d.title else r)
            for e in wo.edges:
                if e.src == did and e.relation == "produces" and e.dst in wo.nodes:
                    p = wo.nodes[e.dst]
                    pr = (p.content or p.pod_ref or "").strip()
                    if pr:
                        out.append(pr)
        return "\n".join(out)


def get_tool_class():
    return CreateWorkNotificationTool
