import json

from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.pydantic_classes import Message


class SummaryAgent(Agent):
    """
    Agent subtype for summary/policy actions.

    It records structured decisions under a non-history message type so planner
    recent_history remains focused on actionable tool/agent outcomes.
    """

    def _create_response_message(self, result_dict: dict):
        if not isinstance(result_dict, dict):
            raise TypeError(f"[{self.name}] Expected dict in _create_response_message, got {type(result_dict)}")

        try:
            result_json = json.dumps(result_dict)
        except TypeError as e:
            raise TypeError(f"[{self.name}] Non serializable summary result: {e}") from e

        msg = Message(
            data_type="summary_action",
            sub_data_type=["policy_action"],
            sender=self.name,
            receiver="Blackboard",
            content=result_json,
            metadata={"context_suppressed": True},
        )
        self.blackboard.add_msg(msg)
