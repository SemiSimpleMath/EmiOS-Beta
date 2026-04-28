from app.assistant.event_hub.event_hub import EventHub
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ProgressEmitter:
    """
    Emits high-signal planner progress facts for UI.
    """

    def __init__(self, *, event_hub: EventHub) -> None:
        self._event_hub = event_hub

    def emit_planner_decision(self, agent, result_dict: dict) -> None:
        if not (
            str(agent.name).endswith("::planner")
            or str(agent.name).endswith(":planner")
            or str(agent.name).endswith("_planner")
        ):
            return

        task = agent.blackboard.get_state_value("task")
        manager_name = ""
        try:
            manager_name = getattr(agent.parent, "name", "") if agent.parent is not None else ""
        except Exception:
            logger.debug("Could not read agent.parent.name", exc_info=True)
            manager_name = ""

        fact = {
            "kind": "planner_decision",
            "agent": agent.name,
            "manager": manager_name,
            "task": task,
            "action": result_dict.get("action"),
            "action_input": result_dict.get("action_input"),
            "learned": (result_dict.get("summary") or "").strip() if isinstance(result_dict.get("summary"), str) else "",
            "action_count": agent.blackboard.get_state_value("action_count"),
        }
        self._event_hub.publish(
            Message(
                sender=agent.name,
                receiver=None,
                event_topic="agent_progress_fact",
                data=fact,
            )
        )
