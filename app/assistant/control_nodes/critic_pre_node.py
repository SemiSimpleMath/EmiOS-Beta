from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.history_formatting import format_recent_history
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import set_resume_target

logger = get_logger(__name__)


class CriticPreNode(ControlNode):
    """
    Pre-critic deterministic router.

    Responsibilities:
    - Evaluate whether critic should run for the current planned call.
    - Build and publish a structured critic payload.
    - Route to critic agent or continue path.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        critic_agent = self._required_str(cfg, "critic_agent")
        critic_capture_node = self._optional_node(cfg, "critic_capture_node")
        continue_agent = self._optional_node(cfg, "continue_agent")
        payload_key = self._optional_str(cfg, "payload_key", "critic_payload")
        payload_history_messages = int(cfg.get("payload_recent_history_messages", 80) or 80)
        if payload_history_messages <= 0:
            raise ValueError("payload_recent_history_messages must be > 0")

        planned_call = self._resolve_planned_call()
        subject_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(subject_agent, str) or not subject_agent.strip():
            raise ValueError(f"[{self.name}] last_agent must be a non-empty string before critic pre route.")
        subject_agent = subject_agent.strip()
        if subject_agent == self.name:
            raise ValueError(f"[{self.name}] last_agent cannot be critic_pre_node itself.")

        if not bool(cfg.get("enabled", False)):
            if isinstance(continue_agent, str):
                self.blackboard.update_state_value("next_agent", continue_agent)
            else:
                self.blackboard.update_state_value("next_agent", None)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        should_run, reason = self._should_run(cfg=cfg, subject_agent=subject_agent)
        if not should_run:
            if isinstance(continue_agent, str):
                self.blackboard.update_state_value("next_agent", continue_agent)
            else:
                self.blackboard.update_state_value("next_agent", None)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        payload = self._build_payload(
            cfg=cfg,
            subject_agent=subject_agent,
            planned_call=planned_call,
            trigger_reason=reason,
            max_messages=payload_history_messages,
        )
        self.blackboard.update_state_value(payload_key, payload)
        logger.debug("[%s] critic triggered reason=%s", self.name, reason)
        action_input = planned_call.get("action_input")
        normalized_args = self._resolve_planned_arguments_for_critic(
            planned_arguments=planned_call.get("arguments"),
            action_input=action_input,
        )
        self.blackboard.update_state_value("critic_subject_agent", subject_agent)
        self.blackboard.update_state_value("critic_intended_action_input", action_input)
        self.blackboard.update_state_value("planned_tool_name", planned_call.get("name"))
        self.blackboard.update_state_value("planned_tool_arguments", normalized_args)

        if isinstance(continue_agent, str):
            set_resume_target(self.blackboard, continue_agent)
        else:
            set_resume_target(self.blackboard, None)
        next_critic_agent = critic_capture_node if isinstance(critic_capture_node, str) else critic_agent
        self.blackboard.update_state_value("next_agent", next_critic_agent)
        self.blackboard.update_state_value("last_agent", self.name)

    def _build_payload(
        self,
        *,
        cfg: dict,
        subject_agent: str,
        planned_call: dict,
        trigger_reason: str,
        max_messages: int,
    ) -> dict:
        scope_id = self.blackboard.get_current_scope_id()
        messages = self.blackboard.get_messages_for_scope(scope_id) if scope_id else []
        history = format_recent_history(messages or [])
        subject_steps = self._subject_steps(subject_agent)
        return {
            "subject_agent": subject_agent,
            "subject_steps": subject_steps,
            "subject_action": planned_call.get("name"),
            "subject_action_input": planned_call.get("action_input"),
            "subject_action_arguments": self._resolve_planned_arguments_for_critic(
                planned_arguments=planned_call.get("arguments"),
                action_input=planned_call.get("action_input"),
            ),
            "planned_call": planned_call,
            "trigger_reason": trigger_reason,
            "recent_history": history,
            "include_screenshot": bool(cfg.get("include_screenshot", False)),
        }

    @staticmethod
    def _resolve_planned_arguments_for_critic(*, planned_arguments, action_input):
        if isinstance(planned_arguments, dict):
            return planned_arguments
        if isinstance(action_input, dict):
            return action_input
        return None

    def _resolve_planned_call(self) -> dict:
        action = self.blackboard.get_state_value("action", None)
        payload = self.blackboard.get_state_value("tool_arguments", None)
        action_input = self.blackboard.get_state_value("action_input", None)

        if not isinstance(action, str) or not action.strip():
            raise ValueError(f"[{self.name}] Missing non-empty blackboard action before critic pre route.")
        target_name = action.strip()
        if not isinstance(payload, dict):
            raise ValueError(f"[{self.name}] tool_arguments must be dict before critic pre route.")

        payload_target = payload.get("target_name")
        if not isinstance(payload_target, str) or not payload_target.strip():
            raise ValueError(f"[{self.name}] tool_arguments.target_name must be non-empty string.")
        if payload_target.strip() != target_name:
            raise ValueError(
                f"[{self.name}] action/tool_arguments target mismatch: action='{target_name}', "
                f"tool_arguments.target_name='{payload_target}'."
            )

        normalized_arguments = payload.get("arguments")
        if normalized_arguments is None:
            normalized_arguments = {}
        if not isinstance(normalized_arguments, dict):
            raise TypeError(
                f"[{self.name}] tool_arguments.arguments must be dict, got {type(normalized_arguments)}."
            )

        return {
            "name": target_name,
            "calling_agent": self.blackboard.get_state_value("last_agent", None),
            "action_input": action_input,
            "arguments": normalized_arguments,
        }

    def _should_run(self, *, cfg: dict, subject_agent: str) -> tuple[bool, str]:
        steps = self._subject_steps(subject_agent)
        if steps <= 0:
            return False, "no_steps"

        cadence = int(cfg.get("cadence_every_actions", 5) or 5)
        if cadence <= 0:
            raise ValueError("cadence_every_actions must be > 0")
        if (steps % cadence) == 0:
            return True, "cadence"
        return False, "cadence_skip"

    def _subject_steps(self, subject_agent: str) -> int:
        stats = self.blackboard.get_state_value("manager_agent_steps", {})
        if stats is None:
            return 0
        if not isinstance(stats, dict):
            raise TypeError(f"[{self.name}] manager_agent_steps must be a dict when provided.")
        raw = stats.get(subject_agent, 0)
        if raw is None:
            return 0
        if not isinstance(raw, int):
            raise TypeError(
                f"[{self.name}] manager_agent_steps['{subject_agent}'] must be int, got {type(raw)}."
            )
        if raw < 0:
            raise ValueError(f"[{self.name}] manager_agent_steps['{subject_agent}'] cannot be negative.")
        return raw

    def _cfg(self) -> dict:
        return self._merged_section_node_cfg("critic")
