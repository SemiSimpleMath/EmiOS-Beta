from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _repo_root_from_here() -> Path:
    return get_repo_root()


def _tool_results_dir() -> Path:
    from app.assistant.utils.path_utils import get_uploads_dir
    return get_uploads_dir() / "temp" / "tool_results"


def _safe_join_tool_result_path(tool_result_id: str) -> Path:
    # Only allow our own generated file pattern.
    safe_id = "".join(ch for ch in tool_result_id.strip() if ch.isalnum())
    if not safe_id:
        raise ValueError("tool_result_id is empty/invalid")
    return _tool_results_dir() / f"tool_result_{safe_id}.json"


def _extract_field(obj: Any, path: str) -> Any:
    """
    Simple dotted-path extractor (e.g. "tool_result.data.call_response.result.content").
    Supports dict keys only.
    """
    cur = obj
    for part in (path or "").split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(f"Field path not found at '{part}'")
    return cur


class ReadToolResult(BaseTool):
    """
    Load a previously persisted full ToolResult artifact by id.

    Use sparingly: prefer summaries unless you need exact details (e.g., a prior snapshot ref).
    """

    def __init__(self):
        super().__init__("read_tool_result")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
        tool_result_id = (arguments.get("tool_result_id") or "").strip()
        field = (arguments.get("field") or "").strip()
        contains = (arguments.get("contains") or "").strip()
        # max_chars:
        # - >0: truncate output to this many characters
        # - 0: no truncation (may be large; use with care)
        max_chars = int(arguments.get("max_chars") if arguments.get("max_chars") is not None else 20000)

        if not tool_result_id:
            return ToolResult(result_type="error", content="read_tool_result: 'tool_result_id' is required.")

        try:
            path = _safe_join_tool_result_path(tool_result_id)
            if not path.exists():
                return ToolResult(
                    result_type="error",
                    content=f"read_tool_result: tool result not found for id={tool_result_id}",
                    data={"tool_result_id": tool_result_id},
                )

            raw = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)

            view: Any = data
            if field:
                view = _extract_field(data, field)

            # Prefer returning raw text when the requested field is already a string (e.g. tool_result.content),
            # to avoid JSON-escaping and keep it more readable for the planner.
            if isinstance(view, str):
                text = view
            else:
                text = json.dumps(view, indent=2, default=str, ensure_ascii=True)

            if contains:
                # Keep only matching lines; for non-line text, this still provides a useful "grep-like" view.
                lines = text.splitlines()
                matched = [ln for ln in lines if contains in ln]
                text = "\n".join(matched) if matched else "[no matching lines]"

            if max_chars > 0 and len(text) > max_chars:
                text = text[:max_chars] + "\n\n[truncated]"

            return ToolResult(
                result_type="tool_result_artifact",
                content=text,
                data={
                    "tool_result_id": tool_result_id,
                    "field": field or None,
                    "contains": contains or None,
                    "path": str(path),
                },
            )
        except Exception as e:
            logger.error(f"read_tool_result failed: {e}", exc_info=True)
            return ToolResult(result_type="error", content=f"read_tool_result failed: {e}")


def get_tool_class():
    return ReadToolResult

