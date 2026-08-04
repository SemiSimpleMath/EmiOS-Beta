"""run_work_node — execute ONE work-object node via the work-object runtime (work_on).

The dayflow switchboard's WORK client. The switchboard reads a ready node and, when it's work (not a
notification), routes it here; this runs the node through work_on — which sets the work context, invokes
work_emi_team, and records the result back on the graph. That graph-aware lifecycle is why work can't go
through a plain manager call: the worker has to know which work object / node it owns.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class RunWorkNodeTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("run_work_node")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = CreateDayflowTicketTool._extract_arguments(tool_message)
            work_id = str(args.get("work_id") or "").strip()
            node_id = str(args.get("node_id") or "").strip()
            if not work_id or not node_id:
                raise ValueError("work_id and node_id are required")
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.discharge import discharge_node
            store = get_dayflow_work_store()
            # Scope = the calling invocation's (ScopeAdapter-narrowed) scope — the tool never mints one.
            discharge_node(store, work_id, node_id, scope_context=tool_message.scope_context)
            n = store.load(work_id).nodes.get(node_id)
            status = n.status if n else "missing"
            node = store.load(work_id).nodes.get(node_id)
            result = (getattr(node, "content", "") or "").strip() if node else ""
            return ToolResult(result_type="success",
                              content=result or f"ran node {node_id} -> {status}",
                              data={"status": status, "work_id": work_id, "node_id": node_id})
        except Exception as e:
            logger.error("run_work_node failed: %s", e)
            logger.debug("run_work_node exception", exc_info=True)
            return ToolResult(result_type="error", content=f"run_work_node failed: {e}", data={})


def get_tool_class():
    return RunWorkNodeTool
