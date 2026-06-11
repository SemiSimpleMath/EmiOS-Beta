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

    # ------------------------------------------------------------------
    # Config readers — the three lookup styles used across control nodes.
    # Subclasses implement `_cfg()` as a one-liner over one of these.
    # ------------------------------------------------------------------

    def _flow_section_cfg(self, section: str, *, required: bool = True) -> dict:
        """Read one named section of the manager's flow_config.

        ``required=False`` returns {} instead of raising when the flow
        config or the section is absent/malformed (e.g. optional game
        modes).
        """
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            if required:
                raise ValueError("manager_flow_config must be a dict.")
            return {}
        section_cfg = flow_cfg.get(section)
        if not isinstance(section_cfg, dict):
            if required:
                raise ValueError(f"manager_flow_config.{section} must be a dict.")
            return {}
        return section_cfg

    def _node_cfg(self) -> dict:
        """Read this node's entry from manager_control_node_configs."""
        node_cfg_map = self.blackboard.get_state_value("manager_control_node_configs", {})
        if not isinstance(node_cfg_map, dict):
            raise ValueError("manager_control_node_configs must be a dict.")
        node_cfg = node_cfg_map.get(self.name, {})
        if not isinstance(node_cfg, dict):
            raise ValueError(f"manager_control_node_configs['{self.name}'] must be a dict.")
        if not node_cfg:
            raise ValueError(f"[{self.name}] missing config.")
        return node_cfg

    def _merged_section_node_cfg(self, legacy_section: str) -> dict:
        """flow_config.<legacy_section> overlaid with this node's own
        control_nodes[...].config entry (node config wins). Either source
        may be absent, but the merge must not be empty."""
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        legacy_cfg = flow_cfg.get(legacy_section)
        if legacy_cfg is None:
            legacy_cfg = {}
        if not isinstance(legacy_cfg, dict):
            raise ValueError(f"manager_flow_config.{legacy_section} must be a dict when provided.")

        node_cfg_map = self.blackboard.get_state_value("manager_control_node_configs", {})
        if node_cfg_map is None:
            node_cfg_map = {}
        if not isinstance(node_cfg_map, dict):
            raise ValueError("manager_control_node_configs must be a dict when provided.")
        node_cfg = node_cfg_map.get(self.name, {})
        if node_cfg is None:
            node_cfg = {}
        if not isinstance(node_cfg, dict):
            raise ValueError(f"manager_control_node_configs['{self.name}'] must be a dict when provided.")

        cfg = dict(legacy_cfg)
        cfg.update(node_cfg)
        if not cfg:
            raise ValueError(
                f"[{self.name}] missing {legacy_section} config. Provide "
                f"flow_config.{legacy_section} or control_nodes[{self.name}].config."
            )
        return cfg

    # ------------------------------------------------------------------
    # Config value validators.
    # ------------------------------------------------------------------

    def _required_str(self, cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"[{self.name}] requires non-empty '{key}'.")
        return raw.strip()

    def _optional_str(self, cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"[{self.name}] key '{key}' must be non-empty when provided.")
        return raw.strip()

    def _optional_node(self, cfg: dict, key: str) -> str | None:
        raw = cfg.get(key, None)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"[{self.name}] key '{key}' must be a non-empty string when provided.")
        return raw.strip()

    def _resolve_resume_agent(self, default_resume_agent: str) -> str:
        """Resume target from pipeline state, else the configured default."""
        from app.assistant.utils.pipeline_state import get_resume_target

        resume_target = get_resume_target(self.blackboard)
        if isinstance(resume_target, str) and resume_target.strip():
            return resume_target.strip()
        return default_resume_agent

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
