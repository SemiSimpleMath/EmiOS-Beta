from __future__ import annotations

from typing import Any, Dict, List


# Preliminary canonical tool contract shape (planner-facing).
# This is intentionally compact and can be refined later.
#
# {
#   "name": str,
#   "description": str,
#   "inputs": [{"name": str, "type": str, "required": bool, "description": str}],
#   "outputs": [{"path": str, "type": str, "description": str}],
#   "arguments_prompt": str,
# }

PRELIMINARY_TOOL_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "set_screen_capture_enabled": {
        "name": "set_screen_capture_enabled",
        "description": "Enable or disable screen capture toggle for screenshot tools.",
        "inputs": [
            {"name": "enabled", "type": "boolean", "required": True, "description": "True to enable, false to disable."},
            {"name": "actor", "type": "string", "required": False, "description": "Actor label, e.g. user/system."},
            {"name": "reason", "type": "string", "required": False, "description": "Reason when disabling."},
        ],
        "outputs": [
            {"path": "content", "type": "text", "description": "Status text."},
            {"path": "data.control.enabled", "type": "boolean", "description": "Resulting toggle state."},
        ],
        "arguments_prompt": '{"enabled": true, "actor": "system"}',
    },
    "capture_monitor_screenshot": {
        "name": "capture_monitor_screenshot",
        "description": "Capture screenshot for a specific monitor and return image path.",
        "inputs": [
            {"name": "monitor_index", "type": "integer", "required": True, "description": "1-based monitor index."},
        ],
        "outputs": [
            {"path": "data.image_path", "type": "image_path", "description": "Absolute path of captured image."},
            {"path": "content", "type": "text", "description": "Capture status text."},
        ],
        "arguments_prompt": '{"monitor_index": 2}',
    },
    "vision_image_describe": {
        "name": "vision_image_describe",
        "description": "Describe an image with concise text.",
        "inputs": [
            {"name": "image_path", "type": "image_path", "required": True, "description": "Path to image file."},
            {"name": "question", "type": "string", "required": False, "description": "Optional guidance question."},
        ],
        "outputs": [
            {"path": "content", "type": "text", "description": "Main description text."},
            {"path": "data.image_path", "type": "image_path", "description": "Echo of input image path."},
        ],
        "arguments_prompt": '{"image_path": "{{steps.cap_m2.data.image_path}}", "question": "What is visible on this monitor?"}',
    },
    "write_text_file": {
        "name": "write_text_file",
        "description": "Write text content to file (overwrite).",
        "inputs": [
            {"name": "file_path", "type": "path", "required": True, "description": "Target file path."},
            {"name": "content", "type": "text", "required": True, "description": "Content to write."},
            {"name": "ensure_newline", "type": "boolean", "required": False, "description": "Ensure trailing newline."},
        ],
        "outputs": [
            {"path": "data.file_path", "type": "path", "description": "Resolved written file path."},
            {"path": "content", "type": "text", "description": "Write status text."},
        ],
        "arguments_prompt": '{"file_path": "resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md", "content": "..." }',
    },
    "find_tool": {
        "name": "find_tool",
        "description": "Find candidate tools by purpose/name and tool kind, including trusted MCP tools.",
        "inputs": [
            {"name": "query", "type": "string", "required": False, "description": "Tool search query."},
            {"name": "tool_kind", "type": "string", "required": False, "description": "all | local | installed_mcp | mcp_available"},
            {"name": "limit", "type": "integer", "required": False, "description": "Max matches."},
        ],
        "outputs": [
            {"path": "data.best_match", "type": "tool_name", "description": "Top matching tool."},
            {"path": "data.matches[].tool_name", "type": "tool_name", "description": "Candidate tool name."},
            {"path": "data.matches[].description", "type": "text", "description": "Short tool description."},
        ],
        "arguments_prompt": '{"query": "bitcoin BTC cryptocurrency price live market data", "tool_kind": "mcp_available", "limit": 5}',
    },
    "install_tool": {
        "name": "install_tool",
        "description": "Install a trusted MCP tool and register it for immediate runtime use.",
        "inputs": [
            {"name": "tool_name", "type": "string", "required": False, "description": "Namespaced tool: mcp::<server_id>::<tool_name>"},
            {"name": "server_id", "type": "string", "required": False, "description": "Trusted MCP server ID."},
            {"name": "mcp_tool_name", "type": "string", "required": False, "description": "Tool name under server_id."},
            {"name": "install_source", "type": "string", "required": False, "description": "Install source metadata."},
            {"name": "launch_id", "type": "string", "required": False, "description": "Optional launch option ID."},
            {"name": "timeout_s", "type": "number", "required": False, "description": "Cache refresh timeout in seconds."},
        ],
        "outputs": [
            {"path": "data.tool_name", "type": "tool_name", "description": "Installed namespaced tool name."},
            {"path": "data.ready_for_planner", "type": "boolean", "description": "True when planner can call the installed tool."},
            {"path": "data.cache_refreshed", "type": "boolean", "description": "True if cache refresh happened during install."},
        ],
        "arguments_prompt": '{"tool_name": "mcp::<server_id>::<tool_name>", "install_source": "agent", "timeout_s": 15.0}',
    },
    "web_manager": {
        "name": "web_manager",
        "description": "Delegate a web research/navigation task to the dedicated web manager team.",
        "inputs": [
            {"name": "task", "type": "string", "required": True, "description": "Detailed web task to execute."},
            {"name": "information", "type": "string", "required": False, "description": "Optional extra context/instructions."},
        ],
        "outputs": [
            {"path": "content", "type": "text", "description": "Human-readable web task result."},
            {"path": "result_type", "type": "string", "description": "Tool result type."},
        ],
        "arguments_prompt": '{"task": "Research the top 3 options and summarize with sources.", "information": ""}',
    },
}


def planner_contract_view(tool_names: List[str]) -> str:
    lines: List[str] = []
    lines.append("Tool contracts (preliminary planner schema):")
    lines.append("Fields: name, description, inputs, outputs, arguments_prompt")
    lines.append("")
    for name in tool_names:
        c = PRELIMINARY_TOOL_CONTRACTS.get(name)
        if not isinstance(c, dict):
            continue
        lines.append(f"## {name}")
        lines.append(f"- description: {c.get('description', '')}")
        inputs = c.get("inputs") if isinstance(c.get("inputs"), list) else []
        lines.append("- inputs:")
        for i in inputs:
            if not isinstance(i, dict):
                continue
            lines.append(
                f"  - {i.get('name')}: type={i.get('type')} required={bool(i.get('required', False))} | {i.get('description', '')}"
            )
        outputs = c.get("outputs") if isinstance(c.get("outputs"), list) else []
        lines.append("- outputs:")
        for o in outputs:
            if not isinstance(o, dict):
                continue
            lines.append(f"  - {o.get('path')}: type={o.get('type')} | {o.get('description', '')}")
        lines.append(f"- arguments_prompt: {c.get('arguments_prompt', '{}')}")
        lines.append("")
    return "\n".join(lines).strip()
