# File: assistant/lib/global_tools/emi_team.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface

class emi_team_manager(BaseTool):
    def __init__(self):
        super().__init__('emi_team')
        self.manager_interface = ManagerInterface('emi_team_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return emi_team_manager