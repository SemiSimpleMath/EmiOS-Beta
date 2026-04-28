from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RoomManager(MultiAgentManager):
    """
    Deterministic room manager.

    Unlike the generic MultiAgentManager loop, this manager does not invoke a
    delegator agent. Routing is done via:
    1) explicit blackboard `next_agent` set by agents/control nodes, otherwise
    2) strict lookup in flow_config.state_map based on `last_agent`.
    """

    def __init__(self, name, manager_config, tool_registry, agent_registry):
        super().__init__(
            name=name,
            manager_config=manager_config,
            tool_registry=tool_registry,
            agent_registry=agent_registry,
        )

    def _pick_next_agent_name(self):
        # Priority 1: explicit next_agent from planner/control nodes.
        next_agent = self.blackboard.get_state_value("next_agent")
        if isinstance(next_agent, str) and next_agent.strip():
            return next_agent.strip()

        # Priority 2: deterministic state-map transition.
        last_agent = self.blackboard.get_state_value("last_agent")
        if not isinstance(last_agent, str) or not last_agent.strip():
            last_agent = "NO_PREVIOUS_AGENT"

        state_map = self.flow_config.get("state_map", {}) if isinstance(self.flow_config, dict) else {}
        if not isinstance(state_map, dict):
            state_map = {}

        mapped = state_map.get(last_agent)
        if isinstance(mapped, str) and mapped.strip():
            return mapped.strip()
        return None

    def run_agent_loop(self):
        logger.info("Starting deterministic room loop for %s", self.name)
        max_cycles = int(self.manager_config.get("max_cycles", 30))
        cycles = 0

        while True:
            try:
                self.blackboard.update_global_state_value("manager_loop_count", cycles)
                self.blackboard.update_global_state_value("manager_loop_number", cycles + 1)
            except Exception:
                logger.error("[%s] Failed to update manager loop counters", self.name)
                logger.debug("failed to update manager loop counters %s exception details", self.name, exc_info=True)

            if self.blackboard.get_state_value("cancelled", False) or self.blackboard.get_state_value("cancel", False):
                logger.warning("[%s] Cancelled. Exiting loop.", self.name)
                return self.handle_exit_error()

            if cycles >= max_cycles:
                logger.warning("[%s] Reached max cycles (%s)", self.name, max_cycles)
                return self.handle_exit_max_limit()

            if self.blackboard.get_state_value("exit", False):
                logger.info("[%s] Exit flag set. Completing.", self.name)
                return self.handle_exit()

            if self.blackboard.get_state_value("error"):
                logger.warning("[%s] Error flag set. Exiting.", self.name)
                return self.handle_exit_error()

            next_agent_name = self._pick_next_agent_name()
            if not next_agent_name:
                self.blackboard.update_state_value(
                    "error",
                    "deterministic routing failed: no next_agent and no state_map match",
                )
                return self.handle_exit_error()

            next_agent_name = self.resolve_role_binding(next_agent_name)
            agent = self.agent_registry.get_agent_instance(next_agent_name)
            if agent is None:
                self.blackboard.update_state_value("error", f"missing agent instance: {next_agent_name}")
                return self.handle_exit_error()

            # Consume stale next_agent before activation to avoid accidental reuse.
            self.blackboard.update_state_value("next_agent", None)
            agent.action_handler(Message(data_type="agent_activation"))
            cycles += 1
