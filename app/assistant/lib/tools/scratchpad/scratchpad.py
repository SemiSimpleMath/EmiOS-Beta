from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


class ScratchpadTool(BaseTool):
    """Write out text during a task. It goes nowhere: the text comes back as the tool's result, so it stays in the
    task's record for the agents that follow, such as a final-answer agent giving a drafted email to the user."""

    def __init__(self):
        super().__init__("scratchpad")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = (tool_message.tool_data or {}).get("arguments", {})
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return make_tool_error(error_code="missing_text", message="scratchpad needs non-empty `text`.")
        return ToolResult(result_type="success", content=text, data={"text": text})


def get_tool_class():
    return ScratchpadTool
