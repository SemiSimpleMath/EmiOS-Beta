#app/assistant/agent_registry/agent_loader.py

import re

from app.assistant.ServiceLocator.service_locator import DI

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class AgentLoader:
    def __init__(self, config_source, blackboard, agent_registry, tool_registry, group_id=None, parent=None):
        """
        Loads and instantiates agents based on a provided manager config.

        Parameters:
        - config_source (dict): The manager config.yaml data.
        - blackboard: The blackboard instance.
        - agent_registry: The registry that holds agent configurations.
        - tool_registry: The registry that manages tools.
        - group_id (Optional): ID for the group this agent belongs to.
        - parent (Optional): The parent manager instance.
        """
        self.config_source = config_source
        self.blackboard = blackboard
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.group_id = group_id
        self.parent = parent

        # Store instantiated agents
        self.agents = {}

    def load_agents(self):
        """
        Reads the manager's config.yaml and initializes the agents.
        Uses the new separated agents/control_nodes format.
        """
        logger.info("🔄 Loading agents...")

        # Load agents
        for agent_cfg in self.config_source["agents"]:
            self._load_single_agent(agent_cfg)
        
        # Load control nodes
        for control_node_cfg in self.config_source["control_nodes"]:
            self._load_single_agent(control_node_cfg)

        logger.info(f"✅ Loaded {len(self.agents)} agents successfully.")

    def _load_single_agent(self, agent_cfg):
        """
        Load a single agent or control node from config.
        """
        agent_name = agent_cfg["name"]
        full_agent_config = self.agent_registry.get_agent_config(agent_name)

        agent_entry = self._resolve_entry_from_declared_class(agent_cfg)
        if not agent_entry:
            agent_entry = self.agent_registry.configs.get(agent_name)
        # Use logging (not print) so Dev Logging Controls can filter this
        logger.info("Registering agent: %s", agent_name)
        
        if not agent_entry:
            logger.error(f"❌ No config found for {agent_name}. Skipping.")
            return

        # Determine agent type from registry
        agent_type = agent_entry.get("type", "agent")  # Default to "agent" if missing
        logger.info("Agent type: %s", agent_type)

        if agent_type == "agent":
            agent_instance = self._load_standard_agent(agent_name, agent_entry["class"])
        elif agent_type == "control_node":
            agent_instance = self._load_control_node(agent_name, agent_entry["class"])
        else:
            logger.warning(f"⚠️ Unknown agent type '{agent_type}' for {agent_name}. Skipping.")
            return

        if agent_instance:
            if isinstance(full_agent_config, dict):
                self.register_agent_events(agent_instance, full_agent_config)
            self.agent_registry.register_agent_instance(agent_name, agent_instance)
        else:
            logger.error("Failed to instantiate agent '%s' (type=%s)", agent_name, agent_type)

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            return ""
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name.strip())
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def _resolve_entry_from_declared_class(self, agent_cfg):
        """
        Resolve manager-local control-node aliases (e.g., 'return_control')
        by using the declared class name (e.g., ReturnControlNode -> return_control_node).
        """
        declared_class = agent_cfg.get("class")
        if not isinstance(declared_class, str) or not declared_class.strip():
            return None
        class_key = self._camel_to_snake(declared_class)
        if not class_key:
            return None
        entry = self.agent_registry.configs.get(class_key)
        if not isinstance(entry, dict):
            return None
        if entry.get("type") != "control_node":
            return None
        return entry

    def _load_standard_agent(self, agent_name, agent_class_name):
        """
        Loads a standard agent from the registry.
        """
        agent_config = self.agent_registry.get_agent_config(agent_name)
        if not agent_config:
            logger.error(f"❌ No config found for agent {agent_name}. Skipping.")
            return None

        logger.info(f"📥 Instantiating agent: {agent_name} (Class: {agent_class_name})")
        return agent_class_name(
            name=agent_name,
            blackboard=self.blackboard,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry,
            llm_params=agent_config.get("llm_params", {}),
            parent=self.parent
        )

    def _load_control_node(self, node_name, node_class):
        """
        Instantiates a control node from its class reference.
        """
        if not node_class:
            logger.error(f"❌ No class found for control node {node_name}. Skipping.")
            return None

        logger.info(f"📥 Instantiating control node: {node_name}")

        return node_class(
            name=node_name,
            blackboard=self.blackboard,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry
        )


    def get_agent_instance(self, agent_name):
        """
        Retrieve an initialized agent instance.
        """
        return self.agents.get(agent_name)

    def get_all_agents(self):
        """
        Returns all instantiated agents.
        """
        return self.agents

    def register_agent_events(self, agent_instance, agent_config):
        events = agent_config.get("events", [])
        for event in events:
            # Create a unique key if needed, e.g. using the agent name:
            event_key = f"{agent_instance.name}:{event}"
            handler = getattr(agent_instance, f"{event}_handler", None)
            if callable(handler):
                DI.event_hub.register_event(event_key, handler)
                logger.info(f"Registered event '{event_key}' for agent {agent_instance.name}")
            else:
                logger.warning(f"Agent {agent_instance.name} config defines event '{event}', but no handler was found.")
