"""Capture ALL planner notes into final_answer_content on return_control.

Sits between the planner's return_control and shared::final_answer in the
playwright manager pipeline. First accumulates the final note (which the
normal accumulator node skips because return_control bypasses it), then
captures all accumulated notes into final_answer_content.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_BB_KEY = "accumulated_notes"


class PlaywrightReturnCaptureNode(ControlNode):

    def action_handler(self, message):
        # The return_control path bypasses the normal accumulator node,
        # so we must accumulate the final note here first.
        note = str(self.blackboard.get_state_value("note", "") or "").strip()
        accumulated = self.blackboard.get_state_value(_BB_KEY, [])
        if not isinstance(accumulated, list):
            accumulated = []
        if note:
            accumulated.append(note)
            self.blackboard.update_state_value(_BB_KEY, accumulated)

        # Now capture all accumulated notes into final_answer_content.
        if accumulated:
            combined = "\n\n---\n\n".join(str(n) for n in accumulated if n)
            if combined:
                self.blackboard.append_state_value("final_answer_content", combined)
                logger.info(
                    "[%s] Captured %d notes (%d chars) into final_answer_content.",
                    self.name, len(accumulated), len(combined),
                )

        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)
