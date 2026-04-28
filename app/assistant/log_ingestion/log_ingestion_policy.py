from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class LogIngestionPolicy:
    """
    Centralized policy for deciding whether a message is persisted to unified logs.
    """

    EXCLUDED_CHAT_TAGS = {
        "agent_notification",
        "entity_card_injection",
        "history_summary",
        "slash_command",
    }
    ROOM_REPLY_TYPES = {"slack", "twilio_sms", "telegram"}

    @classmethod
    def is_room_scoped_payload(cls, payload: Dict[str, Any]) -> bool:
        rid = payload.get("room_id")
        surface = payload.get("room_surface")
        if isinstance(rid, str) and rid.strip():
            return True
        if isinstance(surface, str) and surface.strip():
            return True
        data = payload.get("data")
        if isinstance(data, dict):
            rid2 = data.get("room_id")
            surface2 = data.get("room_surface")
            if isinstance(rid2, str) and rid2.strip():
                return True
            if isinstance(surface2, str) and surface2.strip():
                return True
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            reply_to = metadata.get("reply_to")
            if isinstance(reply_to, dict):
                reply_type = str(reply_to.get("type") or "").strip().lower()
                if reply_type in cls.ROOM_REPLY_TYPES:
                    return True
        return False

    @classmethod
    def should_persist_payload(cls, source: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        src = str(source or "").strip().lower()
        if not src:
            raise ValueError("Log ingestion source must be a non-empty string.")
        if not isinstance(payload, dict):
            raise ValueError(f"Log ingestion payload must be dict, got {type(payload).__name__}.")

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return False, "empty_message"

        # test_mode payloads should never be persisted.
        if bool(payload.get("test_mode", False)):
            return False, "test_mode"

        if src == "chat":
            tags = payload.get("sub_data_type")
            if isinstance(tags, Iterable) and not isinstance(tags, (str, bytes)):
                tagset = {str(t).strip() for t in tags if isinstance(t, str) and str(t).strip()}
            else:
                tagset = set()
            if tagset.intersection(cls.EXCLUDED_CHAT_TAGS):
                return False, "excluded_sub_data_type"
            if cls.is_room_scoped_payload(payload):
                return False, "room_scoped"
            return True, "chat_allowed"

        if src.startswith("room_"):
            return True, "room_allowed"

        return True, "generic_allowed"
