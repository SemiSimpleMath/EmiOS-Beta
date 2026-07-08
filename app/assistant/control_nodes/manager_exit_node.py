from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer

logger = get_logger(__name__)

class ManagerExitNode(ControlNode):
    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def action_handler(self, message: Message):
        logger.info(f"[{self.name}] Manager exit - exiting the entire manager.")
        try:
            existing_final = self.blackboard.get_state_value("final_answer")
            if existing_final is None:
                final_fields = {
                    "final_answer_task": self.blackboard.get_state_value("final_answer_task"),
                    "final_answer_answer": self.blackboard.get_state_value("final_answer_answer"),
                    "final_answer_no_op": self.blackboard.get_state_value("final_answer_no_op"),
                    "final_answer_what_was_done": self.blackboard.get_state_value("final_answer_what_was_done"),
                    "final_answer_interesting_info": self.blackboard.get_state_value("final_answer_interesting_info"),
                    "final_answer_sources": self.blackboard.get_state_value("final_answer_sources"),
                    "final_answer_detail_level": self.blackboard.get_state_value("final_answer_detail_level"),
                    "final_answer_data_list": self.blackboard.get_state_value("final_answer_data_list"),
                }
                # The carry-through fields (pod_references/result_summary) must NOT decide whether we
                # have a real final answer — else a pod-bearing dispatch with no final_answer_* set
                # would discard the dispatched manager's `result` answer text.
                has_state_final = any(v not in (None, "", [], {}) for v in final_fields.values())
                source_payload = final_fields if has_state_final else self.blackboard.get_state_value("result")
                if source_payload is None:
                    # Terminal-agent capture: a flow whose LAST hop is a
                    # form-driven agent routed straight here (no action, so
                    # nothing wrote `result` or final_answer_*). The terminal
                    # agent's structured output IS the manager's answer —
                    # carry it instead of exiting empty (callers used to
                    # scrape the dead manager's audit messages for this).
                    source_payload = self._latest_agent_result_data()
                source_payload = FinalAnswerNormalizer.attach_carry_through(source_payload, self.blackboard)
                if source_payload is not None:
                    normalized = FinalAnswerNormalizer.normalize(source_payload)
                    self.blackboard.update_state_value("final_answer", normalized)
                    self.blackboard.update_state_value("final_answer_raw", source_payload)
                    logger.info("[%s] Materialized final_answer from current scope result.", self.name)
        except Exception as e:
            logger.error("[%s] Failed while materializing final_answer at manager exit: %s", self.name, e)
            logger.debug("failed while materializing final_answer at manager exit %s exception details", self.name, exc_info=True)
            raise

        # Exit the manager
        self.blackboard.update_state_value('exit', True)
        self.blackboard.update_state_value('next_agent', None)
        self.blackboard.update_state_value('last_agent', self.name)

    def _latest_agent_result_data(self):
        """Most recent agent_result message data (a non-empty dict), newest
        first — the terminal agent's structured output."""
        for msg in reversed(self.blackboard.get_messages() or []):
            if getattr(msg, "data_type", None) != "agent_result":
                continue
            data = getattr(msg, "data", None)
            if isinstance(data, dict) and data:
                return data
        return None
