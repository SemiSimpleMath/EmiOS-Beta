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
                state_candidate = {
                    "final_answer_task": self.blackboard.get_state_value("final_answer_task"),
                    "final_answer_answer": self.blackboard.get_state_value("final_answer_answer"),
                    "final_answer_no_op": self.blackboard.get_state_value("final_answer_no_op"),
                    "final_answer_what_was_done": self.blackboard.get_state_value("final_answer_what_was_done"),
                    "final_answer_interesting_info": self.blackboard.get_state_value("final_answer_interesting_info"),
                    "final_answer_sources": self.blackboard.get_state_value("final_answer_sources"),
                    "final_answer_detail_level": self.blackboard.get_state_value("final_answer_detail_level"),
                    "final_answer_data_list": self.blackboard.get_state_value("final_answer_data_list"),
                    "result_summary": self.blackboard.get_state_value("result_summary"),
                    "pod_references": self.blackboard.get_state_value("pod_references"),
                }
                has_state_final = any(v not in (None, "", [], {}) for v in state_candidate.values())
                source_payload = state_candidate if has_state_final else self.blackboard.get_state_value("result")
                if source_payload is not None:
                    normalized = FinalAnswerNormalizer.normalize(source_payload)
                    self.blackboard.update_state_value("final_answer", normalized)
                    logger.info("[%s] Materialized final_answer from current scope result.", self.name)
        except Exception as e:
            logger.error("[%s] Failed while materializing final_answer at manager exit: %s", self.name, e)
            logger.debug("failed while materializing final_answer at manager exit %s exception details", self.name, exc_info=True)
            raise

        # Exit the manager
        self.blackboard.update_state_value('exit', True)
        self.blackboard.update_state_value('next_agent', None)
        self.blackboard.update_state_value('last_agent', self.name)
