from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.text_file_access import is_allowed, resolve_tool_path
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class AppendTextFileTool(BaseTool):
    def __init__(self):
        super().__init__("append_text_file")

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
            logger.info("append_text_file: no file_path provided, defaulting to %s", file_path)
        if not isinstance(content, str):
            return ToolResult(result_type="error", content="Missing required argument: content")

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
            if ensure_newline and not text.endswith("\n"):
                text += "\n"
            with open(resolved, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            return ToolResult(result_type="error", content=f"Failed to append file: {e}")

        return ToolResult(
            result_type="file_write",
            content=f"Appended to {resolved}",
            data={"file_path": str(resolved)},
        )


def get_tool_class():
    return AppendTextFileTool
