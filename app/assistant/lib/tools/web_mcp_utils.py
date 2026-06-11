from __future__ import annotations

from typing import Any, Callable

from app.assistant.lib.mcp.tool_runner import format_mcp_tool_result_content, mcp_stdio_call_tool
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def mcp_call(*, server_entry: dict, tool_name: str, arguments: dict) -> tuple[str, bool, list[dict[str, Any]]]:
    """Call an MCP tool using the server's policy timeout; returns (text, is_error, attachments)."""
    call_resp = mcp_stdio_call_tool(
        server_entry=server_entry,
        tool_name=tool_name,
        arguments=arguments or {},
        timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
    )
    return format_mcp_tool_result_content(call_resp)


def make_mcp_caller(server_entry: dict) -> Callable[[str, dict], tuple[str, bool]]:
    """Bind server_entry; the returned fn(tool_name, arguments) -> (text, is_error) matches the
    mcp_call_fn contract of playwright_snapshot_utils.follow_new_tab_if_opened."""
    def _mcp(tool_name: str, arguments: dict) -> tuple[str, bool]:
        text, is_error, _ = mcp_call(server_entry=server_entry, tool_name=tool_name, arguments=arguments)
        return text, is_error
    return _mcp


def capture_snapshot_text(*, server_entry: dict, snapshot_tool_name: str, tool_label: str) -> str | None:
    """Capture a page snapshot; returns None on error, "" when empty, else the snapshot text."""
    try:
        text_out, is_error, _ = mcp_call(
            server_entry=server_entry,
            tool_name=snapshot_tool_name,
            arguments={},
        )
        if is_error:
            logger.debug("%s snapshot returned error: %s", tool_label, text_out)
            return None
        return text_out if isinstance(text_out, str) and text_out.strip() else ""
    except Exception:
        logger.debug("%s snapshot exception details", tool_label, exc_info=True)
        return None
