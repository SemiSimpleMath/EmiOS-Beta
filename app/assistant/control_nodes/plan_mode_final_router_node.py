from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode


class PlanModeFinalRouterNode(ControlNode):
    """
    Deterministic router for plan-mode chat agents.

    Plan-mode agents are chat-only and should not route to switchboard/tool flow.
    This node converts plan-mode output into final_answer payload and exits.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._required_str(cfg, "source_agent")
        final_node = self._required_str(cfg, "final_node")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")
        planning_done_key = self._optional_str(cfg, "planning_done_key", "planning_done_tf")
        plan_summary_key = self._optional_str(cfg, "plan_summary_key", "plan_summary")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before plan-mode final route, got: {last_agent!r}"
            )

        chat_response = self.blackboard.get_state_value(chat_response_key, "")
        if not isinstance(chat_response, str) or not chat_response.strip():
            raise ValueError(
                f"[{self.name}] {chat_response_key!r} must be a non-empty string for plan-mode response."
            )

        planning_done_tf = bool(self.blackboard.get_state_value(planning_done_key, False))
        plan_summary = self.blackboard.get_state_value(plan_summary_key, "")
        if plan_summary is None:
            plan_summary = ""
        if not isinstance(plan_summary, str):
            raise ValueError(f"[{self.name}] {plan_summary_key!r} must be a string when provided.")

        self.blackboard.update_state_value(
            "result",
            {
                "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                "final_answer_answer": chat_response.strip(),
                "final_answer_what_was_done": "Collaborated in planning mode.",
                "final_answer_interesting_info": "",
                "final_answer_sources": [],
                "planning_mode_done_tf": planning_done_tf,
                "planning_mode_summary": plan_summary.strip(),
            },
        )
        self.blackboard.update_state_value("next_agent", final_node)
        self.blackboard.update_state_value("last_agent", self.name)

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        planning_mode = flow_cfg.get("planning_mode")
        if not isinstance(planning_mode, dict):
            raise ValueError("manager_flow_config.planning_mode must be a dict.")
        return planning_mode

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"plan mode final router requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"plan mode final router key '{key}' must be non-empty when provided.")
        return raw.strip()
