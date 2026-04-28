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
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        action_selector = flow_cfg.get("action_selector")
        if not isinstance(action_selector, dict):
            raise ValueError("manager_flow_config.action_selector must be a dict.")
        return action_selector

    def action_handler(self, message):
        super().action_handler(message)
