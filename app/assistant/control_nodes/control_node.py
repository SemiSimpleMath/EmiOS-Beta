class ControlNode:
    def __init__(self, name, blackboard, agent_registry, tool_registry):
        self.name = name
        self.blackboard = blackboard
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry

    def action_handler(self, message):
        """Each control node implements its own logic here."""
        # self.blackboard.update_state_value('next_agent', None) Don't forget this!
        raise NotImplementedError("ControlNode must define action_handler")

    def _pop_and_route_to_calling_agent(self) -> bool:
        """
        Canonical return-to-caller helper for control nodes.

        Returns True when a call context existed and routing to caller was set.
        Returns False when no call context exists.
        Raises on malformed/invalid call context data.
        """
        current_context = self.blackboard.get_current_call_context()
        if not current_context:
            return False
        if not isinstance(current_context, tuple) or len(current_context) != 3:
            raise RuntimeError(f"[{self.name}] Invalid call context shape: {current_context!r}")

        popped_context = self.blackboard.pop_call_context()
        if not isinstance(popped_context, tuple) or len(popped_context) != 3:
            raise RuntimeError(f"[{self.name}] pop_call_context returned invalid shape: {popped_context!r}")

        calling_agent, _called_agent, _scope_id = popped_context
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            raise RuntimeError(f"[{self.name}] pop_call_context returned empty calling agent: {popped_context!r}")

        self.blackboard.update_state_value("next_agent", calling_agent.strip())
        return True
