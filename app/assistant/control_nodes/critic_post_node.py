from __future__ import annotations

import json

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_resume_target, set_resume_target
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class CriticPostNode(ControlNode):
    """
    Post-critic deterministic router.

    Responsibilities:
    - Read critic decision flag.
    - Route back to subject agent for revise, or continue execution path.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        critic_agent = self._required_str(cfg, "critic_agent")
        continue_agent = self._optional_node(cfg, "continue_agent")
        must_revise_key = self._optional_str(cfg, "must_revise_flag_key", "must_revise_plan")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != critic_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={critic_agent} before critic post route, got: {last_agent!r}"
            )

        subject_agent = self._resolve_subject_agent()
        must_revise = bool(self.blackboard.get_state_value(must_revise_key, False))
        self._publish_subject_feedback(
            subject_agent=subject_agent,
            critic_agent=critic_agent,
            must_revise_key=must_revise_key,
            must_revise=must_revise,
        )
        if must_revise:
            self.blackboard.update_state_value(must_revise_key, False)
            self.blackboard.update_state_value("critic_subject_agent", None)
            set_resume_target(self.blackboard, None)
            self.blackboard.update_state_value("next_agent", subject_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        self.blackboard.update_state_value("critic_subject_agent", None)

        # Restore the planner's pending action. The critic agent's form has
        # ``action: Literal["done"]`` which overwrites the planner's
        # blackboard ``action`` (e.g. "scrape_url") with "done" via
        # _apply_llm_result_to_state. When we route back to tool_caller
        # without a revision, tool_caller would otherwise see
        # action='done' vs tool_arguments.target_name='<planner_tool>' and
        # fail with a target_mismatch. The planner's tool name was stashed
        # by critic_pre_node into ``planned_tool_name`` for exactly this.
        planned_tool_name = self.blackboard.get_state_value("planned_tool_name", None)
        if isinstance(planned_tool_name, str) and planned_tool_name.strip():
            self.blackboard.update_state_value("action", planned_tool_name.strip())

        resume_target = get_resume_target(self.blackboard)
        if not isinstance(resume_target, str) or not resume_target.strip():
            resume_target = continue_agent if isinstance(continue_agent, str) else None
        set_resume_target(self.blackboard, None)
        if isinstance(resume_target, str):
            self.blackboard.update_state_value("next_agent", resume_target.strip())
        else:
            self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    def _resolve_subject_agent(self) -> str:
        subject = self.blackboard.get_state_value("critic_subject_agent", None)
        if isinstance(subject, str) and subject.strip():
            return subject.strip()
        payload = self.blackboard.get_state_value("critic_payload", None)
        if isinstance(payload, dict):
            payload_subject = payload.get("subject_agent")
            if isinstance(payload_subject, str) and payload_subject.strip():
                return payload_subject.strip()
        raise ValueError(f"[{self.name}] Missing critic subject agent in state (critic_subject_agent/critic_payload.subject_agent).")

    def _publish_subject_feedback(
        self,
        *,
        subject_agent: str,
        critic_agent: str,
        must_revise_key: str,
        must_revise: bool,
    ) -> None:
        diagnosis = self.blackboard.get_state_value("critic_diagnosis", None)
        actionable_change = self.blackboard.get_state_value("critic_actionable_change", None)
        diagnosis_tags = self.blackboard.get_state_value("critic_diagnosis_tags", None)
        confidence = self.blackboard.get_state_value("critic_confidence", None)
        planned_tool_name = self.blackboard.get_state_value("planned_tool_name", None)
        planned_tool_arguments = self.blackboard.get_state_value("planned_tool_arguments", None)

        feedback_payload = {
            "critic_agent": critic_agent,
            "subject_agent": subject_agent,
            "must_revise_key": must_revise_key,
            "must_revise_plan": must_revise,
            "planned_tool_name": planned_tool_name,
            "planned_tool_arguments": planned_tool_arguments,
            "critic_diagnosis": diagnosis,
            "critic_actionable_change": actionable_change,
            "critic_diagnosis_tags": diagnosis_tags,
            "critic_confidence": confidence,
        }

        # Record critic intervention in execution trace (if enabled).
        try:
            recorder = self.blackboard.get_state_value("execution_trace_recorder")
            if recorder is not None:
                trigger_reason = str(self.blackboard.get_state_value("playwright_critic_trigger_reason") or "")
                recorder.record_critic_intervention(
                    must_revise=must_revise,
                    diagnosis=str(diagnosis or ""),
                    actionable_change=str(actionable_change or ""),
                    confidence=float(confidence) if confidence is not None else 0.0,
                    trigger_reason=trigger_reason,
                    planned_tool_name=str(planned_tool_name or ""),
                )
        except Exception:
            logger.debug("[%s] Failed to record critic intervention in execution trace", self.name, exc_info=True)

        feedback_json = json.dumps(feedback_payload, ensure_ascii=False)
        self.blackboard.add_msg(
            Message(
                data_type="agent_request",
                sender=self.name,
                receiver=subject_agent,
                content=f"CRITIC RESULT: {feedback_json}",
                metadata={
                    "critic_feedback": True,
                    "critic_agent": critic_agent,
                    "subject_agent": subject_agent,
                },
            )
        )

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        legacy_cfg = flow_cfg.get("critic")
        if legacy_cfg is None:
            legacy_cfg = {}
        if not isinstance(legacy_cfg, dict):
            raise ValueError("manager_flow_config.critic must be a dict when provided.")

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
                f"[{self.name}] missing critic config. Provide flow_config.critic or control_nodes[{self.name}].config."
            )
        return cfg

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"critic post requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"critic post key '{key}' must be non-empty when provided.")
        return raw.strip()

    @staticmethod
    def _optional_node(cfg: dict, key: str) -> str | None:
        raw = cfg.get(key, None)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"critic post key '{key}' must be a non-empty string when provided.")
        return raw.strip()

