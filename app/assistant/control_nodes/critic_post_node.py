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
        return self._merged_section_node_cfg("critic")

