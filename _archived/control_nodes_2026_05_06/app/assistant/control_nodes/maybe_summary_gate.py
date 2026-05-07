from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_pending_tool, get_scratch, set_resume_target, set_scratch

logger = get_logger(__name__)


class MaybeSummaryGate(ControlNode):
    """
    Deterministic summary gate.

    Policy is read from manager_flow_config.gates.summary.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        cfg = self._summary_cfg()
        planner_agent = self._required_str(cfg, "planner_agent")
        tool_arguments_agent = self._required_str(cfg, "tool_arguments_agent")
        summary_node = self._required_str(cfg, "summary_node")
        next_if_skipped = self._optional_str(cfg, "next_if_skipped", tool_arguments_agent)
        summary_resume_target = self._optional_str(cfg, "summary_resume_target", next_if_skipped)
        pending = get_pending_tool(self.blackboard) or {}
        pending_name = pending.get("name")

        # Nothing to gate if there is no pending callable.
        if not isinstance(pending_name, str) or not pending_name.strip():
            self.blackboard.update_state_value("next_agent", planner_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Gate is active only on planner pending-tool path.
        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != planner_agent:
            self.blackboard.update_state_value("next_agent", next_if_skipped or tool_arguments_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        if not bool(cfg.get("enabled", False)):
            self.blackboard.update_state_value("next_agent", next_if_skipped or tool_arguments_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        if not self._should_run(cfg=cfg, planner_agent=planner_agent):
            self.blackboard.update_state_value("next_agent", next_if_skipped or tool_arguments_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        self._publish_summary_runtime_hints(cfg=cfg)
        set_resume_target(self.blackboard, summary_resume_target)
        last_steps_key = self._optional_str(cfg, "summary_last_action_count_key", "summary_last_action_count")
        steps = self._planner_steps(cfg=cfg, planner_agent=planner_agent)
        set_scratch(self.blackboard, last_steps_key, steps)

        self.blackboard.update_state_value("next_agent", summary_node)
        self.blackboard.update_state_value("last_agent", self.name)

    def _summary_cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        gates = flow_cfg.get("gates")
        if not isinstance(gates, dict):
            raise ValueError("manager_flow_config.gates must be a dict.")
        summary = gates.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("manager_flow_config.gates.summary must be a dict.")
        return summary

    def _planner_steps(self, *, cfg: dict, planner_agent: str) -> int:
        action_key_t = self._optional_str(cfg, "action_count_state_key", "{planner_agent}_action_count")
        action_key = action_key_t.format(planner_agent=planner_agent)
        steps = int(self.blackboard.get_state_value(action_key, 0) or 0)
        return steps

    def _should_run(self, *, cfg: dict, planner_agent: str) -> bool:
        steps = self._planner_steps(cfg=cfg, planner_agent=planner_agent)
        if steps <= 0:
            return False

        cadence = int(cfg.get("summary_cadence_every_actions", 4) or 4)
        if cadence <= 0:
            raise ValueError("summary_cadence_every_actions must be > 0")
        if (steps % cadence) != 0:
            return False

        messages = self.blackboard.get_messages() or []
        msg_count = len(messages)
        min_messages = int(cfg.get("summary_min_messages", 140) or 140)
        if min_messages <= 0:
            raise ValueError("summary_min_messages must be > 0")
        if msg_count < min_messages:
            return False

        last_steps_key = self._optional_str(cfg, "summary_last_action_count_key", "summary_last_action_count")
        last_steps = get_scratch(self.blackboard, last_steps_key, None)
        if isinstance(last_steps, int) and (steps - last_steps) < cadence:
            return False
        return True

    def _publish_summary_runtime_hints(self, *, cfg: dict) -> None:
        hints = {
            "summary_context_threshold_messages": int(cfg.get("summary_min_messages", 140) or 140),
            "summary_context_keep_recent_messages": int(cfg.get("summary_keep_recent_messages", 60) or 60),
            "summary_context_max_summary_lines": int(cfg.get("summary_max_summary_lines", 18) or 18),
            "summary_context_max_summary_chars": int(cfg.get("summary_max_summary_chars", 1800) or 1800),
            "summary_context_resume_fallback": self._optional_str(
                cfg, "summary_resume_target", "shared::tool_arguments"
            ),
        }
        raw_keywords = cfg.get("summary_pin_keywords", [])
        if not isinstance(raw_keywords, list):
            raise ValueError("summary_pin_keywords must be a list[str].")
        hints["summary_context_pin_keywords"] = [
            str(x).strip().lower() for x in raw_keywords if isinstance(x, str) and str(x).strip()
        ]
        for key, value in hints.items():
            self.blackboard.update_state_value(key, value)

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"summary gate requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"summary gate key '{key}' must be non-empty when provided.")
        return raw.strip()
