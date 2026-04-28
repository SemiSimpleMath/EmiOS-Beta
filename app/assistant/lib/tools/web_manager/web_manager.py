# File: assistant/lib/global_tools/web_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface

class web_manager(BaseTool):
    """
    Tool to interact with web_manager.
    """

    def __init__(self):
        super().__init__('web_manager')
        self.manager_interface = ManagerInterface('web_manager')


    def execute(self, tool_message):
        """
        Executes WebMultiManager, delegating task handling to ManagerInterface.

        Args:
        - tool_message (ToolMessage): The incoming message triggering the tool execution.

        Returns:
        - ToolResult: The result from web_manager.
        """

        return self.manager_interface.execute(tool_message)

def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return web_manager
