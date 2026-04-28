from app.assistant.event_hub.event_hub import EventHub
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class StatusTracker:
    """
    Runtime status bridge for agent busy/idle publishing.
    """

    def __init__(self, *, event_hub: EventHub) -> None:
        self._event_hub = event_hub

    def set_busy(self, agent_name: str, is_busy: bool) -> None:
        try:
            self._event_hub.set_agent_status(agent_name, bool(is_busy))
        except Exception as e:
            logger.error("[%s] Failed to set busy status=%s: %s", agent_name, bool(is_busy), e)
            logger.debug("[%s] status tracker exception details", agent_name, exc_info=True)
            raise
