"""read_work_object — render a work object to readable text so any agent can answer questions about it
or pull its findings into another task. Read-only; the chat-side counterpart to the /work UI. Use
search_work_objects first if you don't already have the work_id.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)
_MAX_NODE_CHARS = 1500


class ReadWorkObjectTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("read_work_object")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = CreateDayflowTicketTool._extract_arguments(tool_message)
            work_id = str(args.get("work_id") or "").strip()
            if not work_id:
                raise ValueError("work_id is required")
            text = self._render(work_id)
            if text is None:
                return ToolResult(result_type="error",
                                  content=f"work object {work_id!r} not found", data={})
            return ToolResult(result_type="success", content=text, data={"work_id": work_id})
        except Exception as e:
            logger.error("read_work_object failed: %s", e)
            logger.debug("read_work_object exception", exc_info=True)
            return ToolResult(result_type="error", content=f"read_work_object failed: {e}", data={})

    @staticmethod
    def _render(work_id: str):
        """Goal + content, then every step with its status AND its result content (where findings live —
        render_graph_view omits subtask content, so we render it directly)."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        try:
            wo = store.load(work_id)
        except Exception:
            return None
        goal = wo.nodes.get(wo.goal_node_id)
        if goal is None:
            return None
        L = [f"WORK OBJECT {wo.id}  (status: {wo.status})", f"GOAL: {goal.title}"]
        if goal.content:
            L.append((goal.content or "")[:_MAX_NODE_CHARS])
        L.append("\nSTEPS & RESULTS:")
        for n in wo.nodes.values():
            if n.id == wo.goal_node_id:
                continue
            L.append(f"- [{n.status}] {n.type}: {n.title}".rstrip())
            body = (n.content or n.pod_ref or "").strip()
            if body:
                L.append(f"    {body[:_MAX_NODE_CHARS]}")
        return "\n".join(L)


def get_tool_class():
    return ReadWorkObjectTool
