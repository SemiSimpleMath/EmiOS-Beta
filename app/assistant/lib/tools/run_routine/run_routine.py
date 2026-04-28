from __future__ import annotations

import difflib

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.routine_manager import get_routine_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.routine_registry import load_routine_registry, resolve_routine_entry

logger = get_logger(__name__)


class RunRoutineTool(BaseTool):
    def __init__(self):
        super().__init__("run_routine")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        routine_id = (args.get("routine_id") or "").strip()
        routine_query = args.get("routine_query") or args.get("routine_name") or args.get("routine")
        target_date = (args.get("target_date") or "").strip() or None

        if not routine_id:
            query = str(routine_query or tool_message.content or "").strip()
            entry, matches = resolve_routine_entry(query)
            if entry is None:
                if matches:
                    options = ", ".join([m.routine_id for m in matches])
                    return make_tool_error(
                        error_code="routine_name_ambiguous",
                        message=f"Routine name is ambiguous. Matches: {options}",
                        abort_policy="abort_tool",
                        retryable=False,
                        details={"query": query, "matches": [m.routine_id for m in matches]},
                    )
                # Fuzzy suggestions (typos).
                registry = load_routine_registry()
                ids = sorted({e.routine_id for e in registry if e and e.routine_id})
                suggestions = difflib.get_close_matches(query.strip(), ids, n=5, cutoff=0.6) if ids else []
                if suggestions:
                    return make_tool_error(
                        error_code="routine_not_found_suggestions",
                        message=(
                            "Routine not found. Did you mean: "
                            + ", ".join(suggestions)
                            + "?"
                        ),
                        abort_policy="abort_tool",
                        retryable=False,
                        details={"query": query, "suggestions": suggestions},
                    )
                return make_tool_error(
                    error_code="routine_not_found",
                    message="Routine not found. Provide routine_id or a known routine name.",
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"query": query},
                )
            routine_id = entry.routine_id

        try:
            manager = get_routine_manager()
            manager.run_routine_now(routine_id, target_date=target_date)
            return ToolResult(
                result_type="success",
                content=f"Routine '{routine_id}' completed successfully.",
                data_list=[{"routine_id": routine_id, "target_date": target_date}],
            )
        except Exception as e:
            logger.error("run_routine failed for %s", routine_id)
            logger.debug("run_routine failed for  %s exception details", routine_id, exc_info=True)
            return make_tool_error(
                error_code="routine_execution_failed",
                message=f"Routine '{routine_id}' failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"routine_id": routine_id, "target_date": target_date, "error": str(e)},
            )


def get_tool_class():
    return RunRoutineTool

