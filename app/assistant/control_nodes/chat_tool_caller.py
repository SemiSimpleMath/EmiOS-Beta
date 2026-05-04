"""Chat tool caller — basic chat-room dispatch.

Used by the chat-style room managers (room_manager → slack/sms/telegram,
kg_dev_room_manager). Executes the dispatched tool/sub-manager via the
shared ``execute_dispatch`` util and routes forward.

No dayflow integration — this node does not write or close any
dayflow_item rows. The master_room dispatch marker concept is master_room-
specific and lives in MasterRoomToolCaller.
"""
from __future__ import annotations

from app.assistant.control_nodes._tool_caller_util import execute_dispatch
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ChatToolCaller(ControlNode):
    """Execute a single tool/sub-manager and route forward."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        execute_dispatch(
            name=self.name,
            blackboard=self.blackboard,
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
        )
        self.blackboard.update_state_value("last_agent", self.name)
