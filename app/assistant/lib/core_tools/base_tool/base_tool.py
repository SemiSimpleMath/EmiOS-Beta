# File: app/assistant/lib/global_tools/base_tool.py

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

from abc import ABC, abstractmethod

from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


class BaseTool(ABC):
    """
    Abstract base class for all tools.

    Tools that need user approval before they execute declare:
        requires_approval = True

    The approval flow itself lives entirely in ApprovalGateway and is
    orchestrated by ToolCaller. BaseTool is only responsible for execution.
    """
    name: str
    requires_approval: bool = False

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute the tool. Called after approval has been granted (if required).
        """
        pass

    def describe_action(self, tool_message: ToolMessage) -> tuple[str, str]:
        """
        Return (title, detail) describing what this tool is about to do.

        Override in subclasses to produce a human-readable summary shown to the
        user when approval is required. The default formats the raw arguments as
        JSON. Neither this method nor any subclass override should contain any
        approval logic — that lives entirely in ToolCaller and ApprovalGateway.
        """
        import json
        args = (tool_message.tool_data or {}).get('arguments', {})
        try:
            body = json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            body = str(args)
        return (f"Allow {self.name}?", body)
