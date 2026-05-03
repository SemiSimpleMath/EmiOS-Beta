from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class bash_manager(BaseTool):
    def __init__(self):
        super().__init__('bash_manager')
        self.manager_interface = ManagerInterface('bash_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    return bash_manager
