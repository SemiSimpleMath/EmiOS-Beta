"""Chat-room switchboard arguments node.

Used by the basic chat managers (room_manager → slack / sms / telegram).
Translates switchboard agent output into a normalized tool-call payload via
``normalize_switchboard_args`` and routes forward via the state map.

This node has NO dayflow integration. The dayflow dispatch marker that
exists in MasterRoomSwitchboardArgumentsNode is master_room-specific (the
unique relationship between master_room and dayflow); slack/sms/telegram
do not write dispatched tasks into the dayflow item table.
"""
from __future__ import annotations

from app.assistant.control_nodes._switchboard_arguments_util import (
    normalize_switchboard_args,
)
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ChatSwitchboardArgumentsNode(ControlNode):
    """Translate switchboard output into a normalized tool call payload.

    Expected blackboard fields produced by switchboard agent:
    - delegate_to: target manager/tool name
    - task: delegated task
    - task_information: delegated supporting information (optional)
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        normalize_switchboard_args(self.blackboard, name=self.name)
        # Let the state_map handle routing forward.
        self.blackboard.update_state_value("calling_agent", None)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)
