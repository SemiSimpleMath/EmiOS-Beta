from __future__ import annotations

from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.lib.tools.playwright_dom_probe import probe_visible_dom_inputs
from app.assistant.lib.tools.playwright_snapshot_utils import (
    has_input_like_elements,
    make_snapshot_id,
    merge_actionable_with_dom_inputs,
    summarize_actionable_snapshot,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class WebScroll(BaseTool):
    """
    Human-friendly scroll helper for Playwright browser automation.

    Planner-facing API:
    - direction: "down" | "up"
    - amount: pixels per step
    - steps: number of wheel steps
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_WHEEL = "browser_mouse_wheel"
    MCP_SNAPSHOT = "browser_snapshot"

    def __init__(self):
        super().__init__("web_scroll")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        direction = str(args.get("direction") or "down").strip().lower()
        amount = int(args.get("amount", 420) or 420)
        steps = int(args.get("steps", 1) or 1)
        capture_snapshot = bool(args.get("capture_snapshot", True))
        settle_ms = int(args.get("settle_ms", 120) or 120)

        if direction not in {"down", "up"}:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_scroll error: `direction` must be 'down' or 'up'.",
                abort_policy="abort_tool",
                retryable=False,
                details={"direction": direction},
            )
        if amount <= 0:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_scroll error: `amount` must be > 0.",
                abort_policy="abort_tool",
                retryable=False,
                details={"amount": amount},
            )
        if steps <= 0:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_scroll error: `steps` must be > 0.",
                abort_policy="abort_tool",
                retryable=False,
                details={"steps": steps},
            )
        if settle_ms < 0:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_scroll error: `settle_ms` must be >= 0.",
                abort_policy="abort_tool",
                retryable=False,
                details={"settle_ms": settle_ms},
            )

        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            logger.debug("web_scroll failed to resolve MCP server entry", exc_info=True)
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_scroll error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        delta_y = amount if direction == "down" else -amount
        for idx in range(steps):
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_WHEEL,
                arguments={"deltaX": 0, "deltaY": delta_y},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            text_out, is_error, _attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                return make_tool_error(
                    error_code="mcp_call_failed",
                    message=f"web_scroll error: browser_mouse_wheel failed on step {idx + 1}/{steps}: {text_out}",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={
                        "backend": "mcp",
                        "server_id": self.SERVER_ID,
                        "mcp_tool_name": self.MCP_WHEEL,
                        "step": idx + 1,
                        "steps": steps,
                        "deltaY": delta_y,
                    },
                )

            if settle_ms > 0:
                wait_resp = mcp_stdio_call_tool(
                    server_entry=server_entry,
                    tool_name="browser_wait_for",
                    arguments={"time": float(settle_ms) / 1000.0},
                    timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
                )
                wait_text, wait_error, _wait_attachments = format_mcp_tool_result_content(wait_resp)
                if wait_error:
                    return make_tool_error(
                        error_code="mcp_call_failed",
                        message=f"web_scroll error: browser_wait_for failed after step {idx + 1}/{steps}: {wait_text}",
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "backend": "mcp",
                            "server_id": self.SERVER_ID,
                            "mcp_tool_name": "browser_wait_for",
                            "step": idx + 1,
                            "steps": steps,
                            "settle_ms": settle_ms,
                        },
                    )

        snapshot_text = None
        snapshot_id = None
        if capture_snapshot:
            snap_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_SNAPSHOT,
                arguments={},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            snap_text, snap_error, _snap_attachments = format_mcp_tool_result_content(snap_resp)
            if snap_error:
                return make_tool_error(
                    error_code="snapshot_capture_failed",
                    message=f"web_scroll error: browser_snapshot failed after scroll: {snap_text}",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_SNAPSHOT},
                )
            snapshot_text = snap_text if isinstance(snap_text, str) else ""
            snapshot_id = make_snapshot_id(self.name)

        content = (
            "web_scroll:\n"
            f"- direction: {direction}\n"
            f"- amount: {amount}\n"
            f"- steps: {steps}\n"
            f"- settle_ms: {settle_ms}\n"
            f"- status: scrolled"
        )
        result_data: dict[str, Any] = {
            "ok": True,
            "direction": direction,
            "amount": amount,
            "steps": steps,
            "deltaY": delta_y,
            "settle_ms": settle_ms,
        }
        if isinstance(snapshot_text, str):
            summary = summarize_actionable_snapshot(snapshot_text, max_elements=200)
            elements = summary.get("actionable_elements") if isinstance(summary.get("actionable_elements"), list) else []
            if not has_input_like_elements(elements):
                dom_inputs = probe_visible_dom_inputs(server_entry=server_entry, max_items=12)
                elements = merge_actionable_with_dom_inputs(elements=elements, dom_inputs=dom_inputs, max_elements=200)
            result_data["snapshot_id"] = snapshot_id
            result_data["snapshot_url"] = summary.get("url")
            result_data["snapshot_title"] = summary.get("title")
            result_data["actionable_elements"] = elements
            result_data["actionable_total"] = max(int(summary.get("actionable_total") or 0), len(elements))
            result_data["truncated"] = bool(summary.get("truncated", False))
            result_data["hidden_count"] = int(summary.get("hidden_count", 0) or 0)
            content = f"{content}\n- snapshot_id: {snapshot_id}"
            content = f"{content}\n- actionable_elements: {len(elements)}"

        return ToolResult(result_type="web_scroll", content=content, data=result_data)


def get_tool_class():
    return WebScroll

