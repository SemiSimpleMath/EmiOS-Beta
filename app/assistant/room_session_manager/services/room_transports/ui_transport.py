from __future__ import annotations

from typing import Any, Dict

from app.assistant.event_hub.event_hub import EventHub
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData

logger = get_logger(__name__)


class UiRoomTransport:
    def __init__(self, *, event_hub: EventHub) -> None:
        self._event_hub = event_hub

    def send_reply(self, *, room_id: str, body: str, request_id: str = "", metadata: Dict[str, Any] | None = None, widget_data: list | None = None) -> str:
        if not body.strip() and not widget_data:
            return ""
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValueError(f"ui_transport.send_reply requires a non-empty room_id; got {room_id!r}")
        meta = metadata if isinstance(metadata, dict) else {}
        if not isinstance(meta.get("reply_to"), dict):
            # Defensive repair: upstream should have set reply_to. We fall back
            # to the room_id so the message still reaches the UI, but we log it
            # as a WARNING so the gap is visible rather than silently masked.
            # Every occurrence of this warning is an upstream code path that
            # needs to be fixed to attach reply_to at source.
            logger.warning(
                "[ui_transport.send_reply] metadata.reply_to missing; synthesized "
                "default {type=socketio, room_id=%r} — upstream caller should "
                "attach reply_to at source. request_id=%r",
                room_id, request_id or "(none)",
            )
            meta = {**meta, "reply_to": {"type": "socketio", "room_id": room_id}}
        reply_to = meta.get("reply_to", {})
        tts_requested = bool(reply_to.get("tts_requested", False))
        logger.info("[TTS:ui_transport] send_reply room_id=%s tts_requested=%s", room_id, tts_requested)
        assistant_name = get_required_assistant_name()
        msg = UserMessage(
            data_type="user_msg",
            sender=assistant_name,
            receiver=None,
            role="assistant",
            request_id=request_id or None,
            metadata=meta,
            user_message_data=UserMessageData(
                chat=body,
                tts=tts_requested,
                tts_text=body if tts_requested else None,
                widget_data=widget_data or [],
            ),
        )
        msg.event_topic = "socket_emit"
        self._event_hub.publish(msg)
        return ""
