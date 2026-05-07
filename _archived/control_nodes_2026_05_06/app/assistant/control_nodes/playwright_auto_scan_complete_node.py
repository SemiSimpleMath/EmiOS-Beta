from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.pipeline_state import get_flag, set_flag
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlaywrightAutoScanCompleteNode(ControlNode):
    """
    Completes an auto-scan cycle for Playwright by clearing flags and
    returning control to the originating calling agent.
    """

    def action_handler(self, message):  # noqa: ARG002
        try:
            self.blackboard.update_state_value("next_agent", None)
        except Exception as e:
            logger.debug("[%s] Failed to clear next_agent in blackboard: %s", self.name, e, exc_info=True)

        calling_agent = get_flag(self.blackboard, "playwright_auto_scan_calling_agent", None)

        # Clear auto-scan markers.
        try:
            set_flag(self.blackboard, "playwright_auto_scan_in_progress", None)
            set_flag(self.blackboard, "playwright_auto_scan_trigger_tool", None)
            set_flag(self.blackboard, "playwright_auto_scan_prev_tool", None)
        except Exception as e:
            logger.debug("[%s] Failed to clear auto-scan markers in blackboard: %s", self.name, e, exc_info=True)

        # Also clear any pending tab-follow marker.
        try:
            set_flag(self.blackboard, "playwright_auto_tab_follow_in_progress", None)
            set_flag(self.blackboard, "playwright_auto_tab_follow_from", None)
            set_flag(self.blackboard, "playwright_auto_tab_follow_to_index", None)
        except Exception as e:
            logger.debug("[%s] Failed to clear tab-follow markers in blackboard: %s", self.name, e, exc_info=True)

        if isinstance(calling_agent, str) and calling_agent.strip():
            self.blackboard.update_state_value("next_agent", calling_agent)
            logger.debug("[%s] Auto-scan complete; returning control to: %s", self.name, calling_agent)
        else:
            logger.debug("[%s] Auto-scan complete; no calling_agent set.", self.name)

        try:
            self.blackboard.update_state_value("last_agent", self.name)
        except Exception as e:
            logger.debug("[%s] Failed to write last_agent to blackboard: %s", self.name, e, exc_info=True)
