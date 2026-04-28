from pathlib import Path
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
        if ent == rel or fnmatch.fnmatch(rel, ent):
            return True
    return False


class WriteTextFileTool(BaseTool):
    def __init__(self):
        super().__init__("write_text_file")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        file_path = args.get("file_path")
        content = args.get("content")
        ensure_newline = bool(args.get("ensure_newline", True))

        if not isinstance(file_path, str) or not file_path.strip():
            # Default to outputs/ directory with a timestamp-based filename.
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = f"outputs/{ts}_output.md"
            logger.info("write_text_file: no file_path provided, defaulting to %s", file_path)
        if not isinstance(content, str):
            return ToolResult(result_type="error", content="Missing required argument: content")

        resolved = _resolve_path(file_path.strip())
        if resolved is None:
            return ToolResult(result_type="error", content="Invalid or disallowed file_path.")
        allowed_write_files = tool_data.get("allowed_write_files")
        if isinstance(allowed_write_files, list) and not _is_allowed(resolved, allowed_write_files):
            return ToolResult(result_type="error", content="Write blocked by allowed_write_files policy.")

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            text = content
            if ensure_newline and text and not text.endswith("\n"):
                text += "\n"
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            return ToolResult(result_type="error", content=f"Failed to write file: {e}")

        return ToolResult(
            result_type="file_write",
            content=f"Wrote to {resolved}",
            data={"file_path": str(resolved)},
        )


def get_tool_class():
    return WriteTextFileTool
