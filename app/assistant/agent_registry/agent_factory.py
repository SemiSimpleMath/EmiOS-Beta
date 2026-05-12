# app/assistant/agent_registry/agent_factory

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.ServiceLocator.service_locator import DI
import threading

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


def get_agent_registry():
    from app.assistant.agent_registry.agent_registry import AgentRegistry
    return AgentRegistry()

def get_tool_registry():
    from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
    return ToolRegistry()


class AgentFactory:
    def __init__(self, agent_registry, tool_registry):
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry

    def create_agent(self, agent_name, blackboard=None):
        agent_class = self.agent_registry.get_agent_class(agent_name)
        if not agent_class:
            logger.error(f"❌ No class found for agent '{agent_name}'. Cannot instantiate.")
            return None

        logger.info(f"📥 Creating new instance of agent: {agent_name}")

        # Use the provided blackboard or create a new one
        blackboard = blackboard or Blackboard()

        agent = agent_class(
            name=agent_name,
            blackboard=blackboard,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry,
            llm_params=self.agent_registry.get_agent_config(agent_name).get("llm_params", {}),
        )
        # Serialize handler execution per agent instance to avoid concurrent
        # mutations of the same blackboard/state from multiple incoming events.
        agent._event_execution_lock = threading.RLock()

        # Dynamically register events from config using DI.event_hub
        agent_config = self.agent_registry.get_agent_config(agent_name)
        events = agent_config.get('events', [])
        for event in events:
            handler = getattr(agent, f"{event}_handler", None)
            if callable(handler):
                def _serialized_handler(message, _handler=handler, _agent=agent, _event=event):
                    with _agent._event_execution_lock:
                        return _handler(message)
                # AgentFactory is used for system-wide, global event topics (e.g. emi_chat_request).
                # Keep topics un-namespaced to match publishers (routes/services).
                DI.event_hub.register_event(event, _serialized_handler)
                logger.info(f"✅ Registered event '{event}' for agent {agent_name}")
            else:
                logger.warning(f"⚠️ Event '{event}' defined in config but no handler found in {agent_name}.")
        return agent



