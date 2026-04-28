# File: assistant/lib/global_tools/fast_tool_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class fast_tool_manager(BaseTool):
    def __init__(self):
        super().__init__('fast_tool_manager')
        self.manager_interface = ManagerInterface('fast_tool_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return fast_tool_manager
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class fast_tool_manager(BaseTool):
    def __init__(self):
        super().__init__('fast_tool_manager')
        self.manager_interface = ManagerInterface('fast_tool_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return fast_tool_manager
