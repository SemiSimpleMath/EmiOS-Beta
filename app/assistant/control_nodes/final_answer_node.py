import json

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class FinalAnswerNode(ControlNode):
    """
    Finalizes manager output from the current scope `result` and exits manager.

    This enables planner-first flows where the planner emits a direct message
    payload and no additional formatting LLM pass is needed.
    """

    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def action_handler(self, message: Message):
        # Agents with structured output (e.g. response_formatter) write their
        # fields directly to the blackboard as final_answer_* keys. Only a
        # planner-style flow routes its whole output dict into `result`.
        # Prefer the field-level keys when present, then fall back to `result`.
        state_candidate = {
            "final_answer_task": self.blackboard.get_state_value("final_answer_task"),
            "final_answer_answer": self.blackboard.get_state_value("final_answer_answer"),
            "final_answer_no_op": self.blackboard.get_state_value("final_answer_no_op"),
            "final_answer_what_was_done": self.blackboard.get_state_value("final_answer_what_was_done"),
            "final_answer_interesting_info": self.blackboard.get_state_value("final_answer_interesting_info"),
            "final_answer_sources": self.blackboard.get_state_value("final_answer_sources"),
            "final_answer_detail_level": self.blackboard.get_state_value("final_answer_detail_level"),
            "final_answer_data_list": self.blackboard.get_state_value("final_answer_data_list"),
        }
        has_state_final = any(v not in (None, "", [], {}) for v in state_candidate.values())
        raw_result = state_candidate if has_state_final else self.blackboard.get_state_value("result")
        normalized = FinalAnswerNormalizer.normalize(raw_result)

        self.blackboard.update_state_value("final_answer", normalized)
        self.blackboard.update_state_value("exit", True)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

        try:
            self.blackboard.add_msg(
                Message(
                    data_type="agent_result",
                    sender=self.name,
                    receiver="Blackboard",
                    content=json.dumps(normalized),
                    sub_data_type=["result"],
                )
            )
        except Exception as e:
            logger.error("[%s] Failed to log final answer message: %s", self.name, e)
            logger.debug("failed to log final answer message %s exception details", self.name, exc_info=True)
