from app.assistant.event_hub.event_hub import EventHub
from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ChatPublisher:
    """
    Shared publisher for user-facing chat events.
    Ensures consistent request/reply routing metadata across agents.
    """

    def __init__(self, *, event_hub: EventHub) -> None:
        self._event_hub = event_hub

    def publish_chat_to_user(self, *, agent, message) -> None:
        message.event_topic = "socket_emit"

        active_request_id = getattr(agent, "_active_request_id", None)
        if active_request_id and not getattr(message, "request_id", None):
            message.request_id = active_request_id

        reply_to = getattr(agent, "_active_reply_to", None)
        logger.info(
            "[TTS:chat_publisher] agent=%s _active_reply_to=%r",
            getattr(agent, "name", "?"),
            reply_to,
        )
        if isinstance(reply_to, dict):
            if not isinstance(getattr(message, "metadata", None), dict):
                message.metadata = {}
            message.metadata.setdefault("reply_to", reply_to)

        # Determine tts_requested from reply_to or blackboard fallback.
        if not isinstance(getattr(message, "metadata", None), dict):
            message.metadata = {}

        tts_flag = False
        if isinstance(reply_to, dict) and reply_to.get("tts_requested"):
            tts_flag = True
            logger.info("[TTS:chat_publisher] tts_flag=True (from reply_to)")
        if not tts_flag and not message.metadata.get("tts_requested"):
            try:
                bb = getattr(agent, "blackboard", None)
                if bb is not None:
                    tts_flag = bool(bb.get_state_value("tts_requested", False))
                    logger.info("[TTS:chat_publisher] tts_flag=%s (from blackboard)", tts_flag)
            except Exception as e:
                logger.debug("publish_chat_to_user: failed reading tts_requested from blackboard: %s", e, exc_info=True)
                raise

        if tts_flag:
            message.metadata["tts_requested"] = True
            umd = getattr(message, "user_message_data", None)
            if umd is not None and not getattr(umd, "tts", False):
                umd.tts = True
                if not getattr(umd, "tts_text", None):
                    umd.tts_text = getattr(umd, "chat", None) or ""
            logger.info(
                "[TTS:chat_publisher] umd.tts=True umd.tts_text_len=%d room=%s",
                len(getattr(getattr(message, "user_message_data", None), "tts_text", "") or ""),
                reply_to.get("room") if isinstance(reply_to, dict) else "?",
            )
        else:
            logger.info("[TTS:chat_publisher] tts_flag=False — no audio will be generated")

        self._event_hub.publish(message)

