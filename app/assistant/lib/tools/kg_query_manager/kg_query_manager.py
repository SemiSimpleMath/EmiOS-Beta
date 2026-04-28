# File: assistant/lib/tools/kg_query_manager/kg_query_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface

class kg_query_manager(BaseTool):
    def __init__(self):
        super().__init__('kg_query_manager')
        self.manager_interface = ManagerInterface('kg_query_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return kg_query_manager
