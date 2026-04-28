"""
save_daily_summary — persist daily summary data via DailySummaryStorage.

Wraps the summary with metadata and saves to app/daily_summaries/daily_summary_YYYY-MM-DD.json.
The /daily_summary UI route reads from this location.
"""
from __future__ import annotations

import json
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class SaveDailySummary(BaseTool):
    """Save daily summary data to storage for the UI to display."""

    def __init__(self):
        super().__init__("save_daily_summary")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        summary_data = args.get("summary_data")
        date_str = args.get("date_str")

        if not summary_data:
            return make_tool_error(
                error_code="invalid_arguments",
                message="save_daily_summary error: `summary_data` is required.",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": list(args.keys())},
            )

        # If summary_data is a string (e.g., from ${artifact_N} substitution), parse it.
        if isinstance(summary_data, str):
            try:
                summary_data = json.loads(summary_data)
            except json.JSONDecodeError:
                return make_tool_error(
                    error_code="invalid_arguments",
                    message="save_daily_summary error: `summary_data` is not valid JSON.",
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"summary_data_type": "string", "preview": summary_data[:200]},
                )

        if not isinstance(summary_data, dict):
            return make_tool_error(
                error_code="invalid_arguments",
                message=f"save_daily_summary error: `summary_data` must be a dict, got {type(summary_data).__name__}.",
                abort_policy="abort_tool",
                retryable=False,
                details={},
            )

        # Unwrap tool_sequence artifact wrapper if present.
        # The artifact from invoke_agent comes wrapped as:
        #   {content, data: {agent_name, agent_output: {actual summary}}, tool_sequence_results}
        if "tool_sequence_results" in summary_data and isinstance(summary_data.get("data"), dict):
            agent_output = summary_data["data"].get("agent_output")
            if isinstance(agent_output, dict):
                summary_data = agent_output

        try:
            from app.assistant.maintenance_manager.daily_summary_storage import DailySummaryStorage
            storage = DailySummaryStorage()
            filepath = storage.save_daily_summary(summary_data, date_str=date_str)

            logger.info("save_daily_summary: saved to %s", filepath)
            return ToolResult(
                result_type="save_daily_summary",
                content=f"Daily summary saved to {filepath}",
                data={"filepath": filepath, "date_str": date_str or "today"},
            )
        except Exception as e:
            logger.error("save_daily_summary failed: %s", e, exc_info=True)
            return make_tool_error(
                error_code="save_failed",
                message=f"save_daily_summary error: {e}",
                abort_policy="abort_tool",
                retryable=True,
                details={},
            )


def get_tool_class():
    return SaveDailySummary
