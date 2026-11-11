from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.text_file_access import is_allowed, resolve_tool_path
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


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
            # Say which of the two it is. Reporting a wrong-typed `content` as "missing" sent
            # the 2026-09-13 morning_briefing diagnosis in the wrong direction for months: the
            # argument was present, it was a dict.
            return ToolResult(
                result_type="error",
                content=("Missing required argument: content" if content is None else
                         f"Argument 'content' must be text, got {type(content).__name__}"))

        target = resolve_tool_path(file_path.strip())
        if target is None:
            return ToolResult(result_type="error", content="Invalid or disallowed file_path.")
        resolved, logical = target
        allowed_write_files = tool_data.get("allowed_write_files")
        if isinstance(allowed_write_files, list) and not is_allowed(logical, allowed_write_files):
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
