from __future__ import annotations

from app.assistant.control_nodes.chat_task_router_node import ChatTaskRouterNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ActionSelectorRouterNode(ChatTaskRouterNode):
    """
    Dayflow-specific router for the action_selector agent.

    Routes everything to the switchboard — one path for all dispatches.
    Overrides _cfg to read from flow_config.action_selector instead of
    flow_config.chat_gate.
    """

    def _cfg(self) -> dict:
        return self._flow_section_cfg("action_selector")

    def action_handler(self, message):
        super().action_handler(message)
