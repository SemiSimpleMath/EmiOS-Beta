# File: assistant/lib/tools/playwright_manager/playwright_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class playwright_manager(BaseTool):
    """
    Tool to interact with playwright_manager.
    """
    requires_approval = True

    def __init__(self):
        super().__init__('playwright_manager')
        self.manager_interface = ManagerInterface('playwright_manager')

    def describe_action(self, tool_message):
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        task_preview = str(args.get("task") or "").strip()
        if len(task_preview) > 180:
            task_preview = task_preview[:180] + "..."
        return (
            "Allow Playwright browser automation?",
            (
                "This can click, type, submit forms, and trigger external side effects on websites.\n"
                f"Requested task: {task_preview or '[no task provided]'}"
            ),
        )

    def execute(self, tool_message):
        """
        Executes Playwright manager, delegating task handling to ManagerInterface.

        Args:
        - tool_message (ToolMessage): The incoming message triggering the tool execution.

        Returns:
        - ToolResult: The result from playwright_manager.
        """
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return playwright_manager
