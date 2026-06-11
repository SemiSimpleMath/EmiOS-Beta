from __future__ import annotations

import re

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import format_mcp_tool_result_content, mcp_stdio_call_tool
from app.assistant.lib.tools.playwright_dom_probe import probe_visible_dom_inputs
from app.assistant.lib.tools.playwright_snapshot_utils import (
    follow_new_tab_if_opened,
    has_input_like_elements,
    make_snapshot_id,
    merge_actionable_with_dom_inputs,
    summarize_actionable_snapshot,
)
from app.assistant.lib.tools.web_mcp_utils import make_mcp_caller
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class WebClickXySnapshot(BaseTool):
    SERVER_ID = "npm/playwright-mcp"
    MCP_CLICK_XY = "browser_mouse_click_xy"
    MCP_SNAPSHOT = "browser_snapshot"

    def __init__(self):
        super().__init__("web_click_xy_snapshot")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        x = args.get("x")
        y = args.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_click_xy_snapshot error: numeric `x` and `y` are required.",
                abort_policy="abort_tool",
                retryable=False,
                details={"x": x, "y": y},
            )

        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            logger.debug("web_click_xy_snapshot failed to resolve MCP server", exc_info=True)
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_click_xy_snapshot error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        _mcp = make_mcp_caller(server_entry)

        # Record tab count before click so we can detect new-tab navigation.
        tabs_text_before, tabs_err_before = _mcp("browser_tabs", {"action": "list"})
        tab_count_before = len([
            ln for ln in (tabs_text_before or "").splitlines()
            if re.match(r"\s*-\s*\d+\s*:", ln)
        ]) if not tabs_err_before else 0

        click_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_CLICK_XY,
            arguments={"x": float(x), "y": float(y)},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        click_text, click_error, _click_attachments = format_mcp_tool_result_content(click_resp)
        if click_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_click_xy_snapshot error: browser_mouse_click_xy failed: {click_text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_CLICK_XY},
            )

        followed_new_tab, new_tab_index = follow_new_tab_if_opened(
            server_entry=server_entry,
            tab_count_before=tab_count_before,
            mcp_call_fn=_mcp,
        )
        if followed_new_tab:
            logger.debug("web_click_xy_snapshot: new tab detected (index=%s), switched automatically", new_tab_index)

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
                message=f"web_click_xy_snapshot error: browser_snapshot failed after click: {snap_text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_SNAPSHOT},
            )

        snapshot_id = make_snapshot_id(self.name)
        summary = summarize_actionable_snapshot(snap_text if isinstance(snap_text, str) else "", max_elements=200)
        elements = summary.get("actionable_elements") if isinstance(summary.get("actionable_elements"), list) else []
        if not has_input_like_elements(elements):
            dom_inputs = probe_visible_dom_inputs(server_entry=server_entry, max_items=12)
            elements = merge_actionable_with_dom_inputs(elements=elements, dom_inputs=dom_inputs, max_elements=200)
        content = f"web_click_xy_snapshot:\n- x: {float(x)}\n- y: {float(y)}\n- status: clicked"
        if followed_new_tab:
            content = f"{content}\n- new_tab: followed (tab index {new_tab_index})"
        content = f"{content}\n- snapshot_id: {snapshot_id}"
        content = f"{content}\n- actionable_elements: {len(elements)}"
        return ToolResult(
            result_type="web_click_xy_snapshot",
            content=content,
            data={
                "ok": True,
                "x": float(x),
                "y": float(y),
                "followed_new_tab": followed_new_tab,
                "new_tab_index": new_tab_index,
                "snapshot_id": snapshot_id,
                "snapshot_url": summary.get("url"),
                "snapshot_title": summary.get("title"),
                "actionable_elements": elements,
                "actionable_total": max(int(summary.get("actionable_total") or 0), len(elements)),
                "truncated": bool(summary.get("truncated", False)),
                "hidden_count": int(summary.get("hidden_count", 0) or 0),
            },
        )


def get_tool_class():
    return WebClickXySnapshot
