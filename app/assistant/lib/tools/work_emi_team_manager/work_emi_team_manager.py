from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class work_emi_team_manager(BaseTool):
    """Tool to interact with work_emi_team_manager — the node-graph work team.

    Identical in shape to every other manager wrapper. Because work_emi_team_manager is
    `node_aware`, ManagerInterface decides how to run it: ON the node named in the arguments
    when the dayflow dispatch calls it, on a fresh child node when it is delegated to from
    inside a work run, and as an ordinary one-shot sub-manager otherwise. The caller does not
    need to know which — it just calls a manager.

    This wrapper is the file that was missing. Without it the manager had no tool entry at
    all, and the dayflow work lane reached it through a private path of its own
    (`run_work_node` -> `discharge_node`) that hand-rolled the node handover and recorded the
    result itself — the one manager in the system not called the way managers are called.
    """

    def __init__(self):
        super().__init__('work_emi_team_manager')
        self.manager_interface = ManagerInterface('work_emi_team_manager')

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """Returns the class for the tool. Required by the tool registry."""
    return work_emi_team_manager
