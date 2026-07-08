# Note to coding agents: This file should not be modified without user permission.
from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class Delegator(Agent):
    """
    Deterministic flow router.

    Delegator intentionally does not implement policy gates.
    All conditional policy (critic/summary/etc.) must live in explicit control nodes.
    """

    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent=parent)
        self.flow_config = None

    def action_handler(self, message: Message):
        flow_data = message.data if isinstance(message.data, dict) else {}
        self.flow_config = flow_data.get("flow_config")
        if not isinstance(self.flow_config, dict):
            raise ValueError(f"[{self.name}] flow_config must be a dict.")

        if message.content:
            self.blackboard.add_msg(message)

        last_agent = self.blackboard.get_state_value("last_agent")
        logger.info("[%s] Last agent: %s", self.name, last_agent)

        # Honor explicit routing already chosen by an agent/control node.
        next_agent = self.blackboard.get_state_value("next_agent")
        if isinstance(next_agent, str) and next_agent.strip():
            logger.info("[%s] next_agent already set to: %s, returning early", self.name, next_agent)
            return

        next_node = self.pick_next_agent(last_agent)
        if isinstance(next_node, str) and next_node.strip():
            logger.info("[%s] Delegating to: %s", self.name, next_node)
            self.blackboard.update_state_value("next_agent", next_node.strip())
            return

        logger.error("[%s] Missing flow transition for last_agent=%s", self.name, last_agent)
        self.blackboard.update_state_value("error_message", "Delegator routing failed: missing state_map entry")
        self.blackboard.update_state_value("error", True)
        self.blackboard.update_state_value("last_agent", self.name)

    def pick_next_agent(self, last_agent: str) -> str | None:
        key = last_agent if isinstance(last_agent, str) and last_agent else "NO_PREVIOUS_AGENT"
        state_map = self.flow_config.get("state_map", {})
        if not isinstance(state_map, dict):
            raise ValueError(f"[{self.name}] flow_config.state_map must be a dict.")
        next_agent = state_map.get(key)

        return next_agent if isinstance(next_agent, str) and next_agent else None
