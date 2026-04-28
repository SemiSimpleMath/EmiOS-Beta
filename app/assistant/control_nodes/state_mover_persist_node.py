from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class StateMoverPersistNode(ControlNode):
    """
    Post-guard node after state_transition_guard_node.

    state_transition_guard_node already saves mutations to the DB.
    This node sets state_mutations_persisted_tf so post_room_finalize_node
    skips re-applying the same mutations and avoids the from_state mismatch
    warning.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("state_mutations_persisted_tf", True)

        self.blackboard.update_state_value("last_agent", self.name)
