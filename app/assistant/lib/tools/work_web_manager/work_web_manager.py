# File: assistant/lib/tools/work_web_manager/work_web_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface

class work_web_manager(BaseTool):
    """
    Tool to interact with work_web_manager — the node-graph web team. Identical in shape to the
    web_manager tool wrapper; because work_web_manager is `node_aware`, ManagerInterface runs it ON a
    fresh child graph node when called from inside a WorkObject context (and as an ordinary one-shot
    sub-manager otherwise). The caller does not need to know which — it just calls a manager.
    """

    def __init__(self):
        super().__init__('work_web_manager')
        self.manager_interface = ManagerInterface('work_web_manager')


    def execute(self, tool_message):
        """
        Executes work_web_manager, delegating task handling to ManagerInterface.

        Args:
        - tool_message (ToolMessage): The incoming message triggering the tool execution.

        Returns:
        - ToolResult: The result from work_web_manager.
        """

        return self.manager_interface.execute(tool_message)

def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return work_web_manager
