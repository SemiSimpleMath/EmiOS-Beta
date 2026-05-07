"""Accumulate planner notes across turns so extracted data is never lost.

Runs after each planner turn. Appends the planner's note to a durable
blackboard list. The planner sees this list via user_context_items as
'accumulated_notes', giving it access to all previously extracted data
(headlines, prices, etc.) regardless of summary compression.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_BB_KEY = "accumulated_notes"


class PlaywrightNoteAccumulatorNode(ControlNode):

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        note = str(self.blackboard.get_state_value("note", "") or "").strip()
        if note:
            existing = self.blackboard.get_state_value(_BB_KEY, [])
            if not isinstance(existing, list):
                existing = []
            existing.append(note)
            self.blackboard.update_state_value(_BB_KEY, existing)
            logger.debug(
                "[%s] Accumulated note (%d chars, total %d notes).",
                self.name, len(note), len(existing),
            )

        self.blackboard.update_state_value("last_agent", self.name)
