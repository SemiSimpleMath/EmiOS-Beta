"""
MCP tool execution for ToolCaller.

Self-contained: takes tool config + arguments, returns a ToolResult.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import (
    mcp_stdio_call_tool,
    format_mcp_tool_result_content,
    sanitize_mcp_call_response_for_history,
)
from app.assistant.utils.pydantic_classes import ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def execute_mcp_tool_call(
    *,
    tool_name: str,
    tool_config: dict,
    arguments: dict,
    tool_registry: Any,
) -> ToolResult:
    """
    Execute an MCP-backed tool call and convert the result into a ToolResult.
    """
    server_id = tool_config.get("mcp_server_id")
    mcp_tool_name = tool_config.get("mcp_tool_name")
    if not server_id or not mcp_tool_name:
        return make_tool_error(
            error_code="mcp_tool_misconfigured",
            message=f"MCP tool misconfigured: missing mcp_server_id or mcp_tool_name for {tool_name}",
            abort_policy="abort_tool",
            retryable=False,
            details={"tool_name": tool_name, "tool_config": tool_config},
        )

    server_entry = tool_registry.get_mcp_server_entry(server_id)
    if not server_entry:
        return make_tool_error(
            error_code="mcp_server_not_loaded",
            message=f"MCP server entry not loaded: {server_id}",
            abort_policy="abort_tool",
            retryable=False,
            details={"tool_name": tool_name, "server_id": server_id},
        )

    if not isinstance(arguments, dict):
        return make_tool_error(
            error_code="mcp_arguments_invalid",
            message=f"MCP call requires dict arguments for tool '{tool_name}', got {type(arguments)}.",
            abort_policy="abort_tool",
            retryable=False,
            details={"backend": "mcp", "tool_name": tool_name},
        )
    args_obj = dict(arguments)
    # If structured outputs forced nullable-but-required fields, drop nulls before sending
    # to the MCP server so it receives only explicitly provided arguments.
    args_obj = {k: v for k, v in args_obj.items() if v is not None}
    # Some search APIs treat `order` as meaningful only when `sort` is provided.
    # Avoid passing `order` alone (can trigger server-side validation bugs).
    if ("sort" not in args_obj or not args_obj.get("sort")) and "order" in args_obj:
        args_obj.pop("order", None)

    # Timeout policy
    timeout_s = 20.0
    try:
        pol = server_entry.get("policy") if isinstance(server_entry, dict) else None
        if isinstance(pol, dict) and isinstance(pol.get("call_timeout_seconds"), int):
            timeout_s = float(pol["call_timeout_seconds"])
    except Exception:
        logger.debug("[mcp_tool_executor] Failed to parse MCP timeout policy. Using default timeout.", exc_info=True)

    try:
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=str(mcp_tool_name),
            arguments=args_obj or {},
            timeout_s=timeout_s,
        )
        text, is_error, attachments = format_mcp_tool_result_content(call_resp)
        call_resp_history = sanitize_mcp_call_response_for_history(call_resp, attachments)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_error",
                message=text,
                abort_policy="abort_tool",
                retryable=False,
                details={
                    "backend": "mcp",
                    "server_id": server_id,
                    "mcp_tool_name": mcp_tool_name,
                    "arguments_sent": args_obj,
                    "call_response": call_resp_history,
                    "attachments": attachments,
                },
            )
        return ToolResult(
            result_type="tool_result",
            content=text,
            data={
                "backend": "mcp",
                "server_id": server_id,
                "mcp_tool_name": mcp_tool_name,
                "arguments_sent": args_obj,
                # IMPORTANT: store sanitized response (no base64 blobs) so planner history stays small.
                "call_response": call_resp_history,
                "attachments": attachments,
            },
        )
    except Exception as e:
        return make_tool_error(
            error_code="mcp_call_failed",
            message=f"MCP call failed ({server_id}/{mcp_tool_name}): {e}",
            abort_policy="abort_tool",
            retryable=False,
            details={"backend": "mcp", "server_id": server_id, "mcp_tool_name": mcp_tool_name},
        )
