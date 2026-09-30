# File: assistant/lib/tools/work_personal_admin_manager/work_personal_admin_manager.py

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface


class work_personal_admin_manager(BaseTool):
    """
    Tool to interact with work_personal_admin_manager — the node-graph personal admin team (Gmail,
    Calendar, Tasks, Google Docs/Drive). Identical in shape to the personal_admin_manager wrapper;
    because the manager is `node_aware`, ManagerInterface runs it ON a fresh child graph node when
    called from inside a WorkObject context. Without this wrapper the manager existed but was never
    a tool, and work tasks needing Gmail fell back to the browser (2026-09-24, 2026-09-29).
    """

    def __init__(self):
        super().__init__("work_personal_admin_manager")
        self.manager_interface = ManagerInterface("work_personal_admin_manager")

    def execute(self, tool_message):
        return self.manager_interface.execute(tool_message)


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return work_personal_admin_manager
