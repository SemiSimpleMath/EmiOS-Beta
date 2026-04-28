from pathlib import Path
from typing import Optional
import fnmatch

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _repo_root() -> Path:
    return get_repo_root()


def _resolve_path(path_str: str) -> Path | None:
    try:
        raw = Path(path_str)
    except Exception:
        return None
    root = _repo_root()
    if raw.is_absolute():
        try:
            raw.relative_to(root)
        except Exception:
            return None
        return raw
    return (root / raw).resolve()


def _is_allowed(resolved: Path, allowed: list[str] | None) -> bool:
    if not allowed:
        return True
    try:
        root = _repo_root()
        rel = resolved.relative_to(root).as_posix()
    except Exception:
        return False
    for entry in allowed:
        if not isinstance(entry, str) or not entry.strip():
            continue
        ent = entry.strip().replace("\\", "/")
        # Exact match or glob match.
        if ent == rel or fnmatch.fnmatch(rel, ent):
            return True
    return False


class ReadTextFileTool(BaseTool):
    def __init__(self):
        super().__init__("read_text_file")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        file_path = args.get("file_path")
        start_marker = args.get("start_marker")
        end_marker = args.get("end_marker")
        include_markers = bool(args.get("include_markers", False))
        max_chars = args.get("max_chars")
        if isinstance(max_chars, int) and max_chars <= 0:
            max_chars = None

        if not isinstance(file_path, str) or not file_path.strip():
            return ToolResult(result_type="error", content="Missing required argument: file_path")

        resolved = _resolve_path(file_path.strip())
        if resolved is None:
            return ToolResult(result_type="error", content="Invalid or disallowed file_path.")
        allowed_read_files = tool_data.get("allowed_read_files")
        if isinstance(allowed_read_files, list) and not _is_allowed(resolved, allowed_read_files):
            return ToolResult(result_type="error", content="Read blocked by allowed_read_files policy.")

        if not resolved.exists():
            return ToolResult(result_type="error", content=f"File does not exist: {resolved}")

        try:
            content = resolved.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(result_type="error", content=f"Failed to read file: {e}")

        if isinstance(start_marker, str) and start_marker:
            start_idx = content.find(start_marker)
            if start_idx < 0:
                return ToolResult(
                    result_type="error",
                    content=f"Start marker not found: {start_marker}",
                )
            start = start_idx if include_markers else start_idx + len(start_marker)
            end = len(content)
            if isinstance(end_marker, str) and end_marker:
                end_idx = content.find(end_marker, start_idx + len(start_marker))
                if end_idx < 0:
                    return ToolResult(
                        result_type="error",
                        content=f"End marker not found: {end_marker}",
                    )
                end = end_idx + (len(end_marker) if include_markers else 0)
            content = content[start:end]

        if isinstance(max_chars, int) and max_chars > 0:
            content = content[:max_chars]

        return ToolResult(
            result_type="file_read",
            content=content,
            data={"file_path": str(resolved)},
        )


def get_tool_class():
    return ReadTextFileTool
