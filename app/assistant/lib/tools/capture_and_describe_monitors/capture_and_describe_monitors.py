from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


def _compact_text(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.split())


def _best_guess_activity(recaps: List[str]) -> str:
    joined = " ".join([r.lower() for r in recaps if isinstance(r, str)])
    if any(k in joined for k in ("vscode", "code", "python", "terminal", "github", "stack trace", "sqlite")):
        return "At keyboard doing software development work."
    if any(k in joined for k in ("slack", "email", "inbox", "message", "chat")):
        return "At keyboard handling messages and coordination."
    if any(k in joined for k in ("browser", "docs", "documentation", "article", "search", "web")):
        return "At keyboard researching and reading in browser."
    return "At keyboard working."


class CaptureAndDescribeMonitorsTool(BaseTool):
    """
    Capture one or more monitors, describe each screenshot, and optionally write
    the latest desktop activity snippet to file.
    """

    def __init__(self):
        super().__init__("capture_and_describe_monitors")

    def _execute_tool(self, tool_name: str, arguments: Dict[str, Any], parent_msg: ToolMessage) -> ToolResult:
        tool_class = DI.tool_registry.get_tool_class(tool_name)
        if tool_class is None:
            return ToolResult(result_type="error", content=f"Tool not found: {tool_name}")

        tool_instance = tool_class()
        tool_data = {"tool_name": tool_name, "arguments": arguments}

        # Preserve task-level file policies for nested writes.
        parent_data = parent_msg.tool_data if isinstance(parent_msg.tool_data, dict) else {}
        if isinstance(parent_data.get("allowed_read_files"), list):
            tool_data["allowed_read_files"] = parent_data.get("allowed_read_files")
        if isinstance(parent_data.get("allowed_write_files"), list):
            tool_data["allowed_write_files"] = parent_data.get("allowed_write_files")
        if parent_data.get("task_spec") is not None:
            tool_data["task_spec"] = parent_data.get("task_spec")

        nested = ToolMessage(
            tool_name=tool_name,
            tool_data=tool_data,
            task=parent_msg.task,
            information=parent_msg.information,
            request_id=parent_msg.request_id,
        )
        result = tool_instance.execute(nested)
        return result if isinstance(result, ToolResult) else ToolResult(result_type="error", content="Invalid nested tool result")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}

        monitor_indices = args.get("monitor_indices") or [2, 3]
        if not isinstance(monitor_indices, list) or not monitor_indices:
            return ToolResult(result_type="error", content="monitor_indices must be a non-empty list")

        indices: List[int] = []
        for raw in monitor_indices:
            try:
                idx = int(raw)
            except Exception:
                return ToolResult(result_type="error", content=f"Invalid monitor index: {raw!r}")
            if idx <= 0:
                return ToolResult(result_type="error", content=f"monitor index must be >= 1, got {idx}")
            indices.append(idx)

        from app.assistant.kg_core.user_identity import get_primary_user_name
        default_question = f"In 1-2 concise sentences, describe what {get_primary_user_name()} is doing on this monitor right now."
        question = _compact_text(args.get("question") or default_question)
        output_path = _compact_text(
            args.get("output_file_path")
            or "resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md"
        )
        write_output = bool(args.get("write_output", True))
        ensure_newline = bool(args.get("ensure_newline", True))

        captures: List[Dict[str, Any]] = []
        recaps: List[str] = []

        for idx in indices:
            cap_res = self._execute_tool(
                "capture_monitor_screenshot",
                {"monitor_index": idx},
                tool_message,
            )
            if cap_res.result_type == "error":
                return ToolResult(
                    result_type="error",
                    content=f"Failed capture for monitor {idx}: {cap_res.content}",
                    data={"monitor_index": idx, "capture_error": cap_res.content},
                )

            image_path = ""
            if isinstance(cap_res.data, dict):
                image_path = _compact_text(cap_res.data.get("image_path"))
            if not image_path:
                return ToolResult(
                    result_type="error",
                    content=f"Capture returned no image path for monitor {idx}.",
                    data={"monitor_index": idx, "capture_result": cap_res.data},
                )

            desc_res = self._execute_tool(
                "vision_image_describe",
                {"image_path": image_path, "question": question},
                tool_message,
            )
            if desc_res.result_type == "error":
                return ToolResult(
                    result_type="error",
                    content=f"Failed description for monitor {idx}: {desc_res.content}",
                    data={"monitor_index": idx, "image_path": image_path, "describe_error": desc_res.content},
                )

            recap = _compact_text(desc_res.content) or "No clear activity detected."
            recaps.append(recap)
            captures.append(
                {
                    "monitor_index": idx,
                    "image_path": image_path,
                    "recap": recap,
                }
            )

        timestamp_local = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        lines = [f"{timestamp_local}"]
        for item in captures:
            lines.append(f"Monitor {item['monitor_index']}: {item['recap']}")
        lines.append(f"Final call: {_best_guess_activity(recaps)}")
        snippet = "\n".join(lines)

        write_result: ToolResult | None = None
        if write_output:
            write_result = self._execute_tool(
                "write_text_file",
                {
                    "file_path": output_path,
                    "content": snippet,
                    "ensure_newline": ensure_newline,
                },
                tool_message,
            )
            if write_result.result_type == "error":
                return ToolResult(
                    result_type="error",
                    content=f"Failed to write activity snippet: {write_result.content}",
                    data={"output_file_path": output_path, "snippet": snippet},
                )

        return ToolResult(
            result_type="tool_result",
            content=snippet,
            data={
                "timestamp_local": timestamp_local,
                "captures": captures,
                "output_file_path": output_path if write_output else None,
                "write_result": write_result.data if write_result else None,
            },
        )


def get_tool_class():
    return CaptureAndDescribeMonitorsTool
