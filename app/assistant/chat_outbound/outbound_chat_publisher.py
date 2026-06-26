"""OutboundChatPublisher — single surface-aware publish path for chat text.

Both the chat narrator (live progress lines, "Delegating to Webby.") and
the chat-gate ack ("Yep — let me check.") need the same thing: deliver
a short message to the user's originating surface. Today there are
multiple ad-hoc paths (event_hub socket_emit for UI, direct
slack_transport.send_reply for Slack, etc.); each consumer that wants
to render outbound chat ends up duplicating the surface dispatch.

This service centralizes the dispatch. Callers pass ``(sender, text,
reply_to)`` and the publisher picks the transport based on
``reply_to.type``. Adding Telegram or SMS narration is one new branch
here, no consumer-side change.

Lives in DI as ``DI.outbound_chat_publisher``. ChatNarrator and
ChatTaskRouterNode (and any future outbound-chat producer) call it
instead of inlining transport-specific code.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    UserMessage,
    UserMessageData,
)

logger = get_logger(__name__)


class OutboundChatPublisher:
    """Route a chat message to the right transport, by ``reply_to.type``."""

    def publish(
        self,
        *,
        sender: str,
        text: str,
        reply_to: Optional[Dict[str, Any]],
        request_id: Optional[str] = None,
        sub_data_type: Optional[list[str]] = None,
    ) -> bool:
        """Publish ``text`` to the surface named in ``reply_to``.

        Returns True if the dispatch was attempted, False if no
        suitable transport was found (no reply_to, unknown type, or
        transport unavailable). Never raises into the caller — outbound
        publishing must not break the host flow.

        ``request_id`` and ``sub_data_type`` are optional; included on
        the wire message for surfaces that thread/correlate by them.
        """
        if not text or not text.strip():
            return False
        if reply_to is None:
            # No destination pinned -> owner UI default (master_room), consistent with
            # EmiEventRelay._resolve_reply_to. A destination-less owner message is a
            # system/autonomous notification; master_room is the live/active UI surface.
            # WARN (not silent) so a caller that should have pinned one stays visible,
            # but DELIVER instead of dropping.
            logger.warning("[outbound] no reply_to — defaulting to master_room (sender=%s)", sender)
            reply_to = {"type": "socketio", "room_id": "master_room"}
        surface_type = str(reply_to.get("type") or "").strip().lower()
        try:
            if surface_type == "socketio":
                return self._publish_socketio(
                    sender=sender, text=text, reply_to=reply_to,
                    request_id=request_id, sub_data_type=sub_data_type,
                )
            if surface_type == "slack":
                return self._publish_slack(
                    sender=sender, text=text, reply_to=reply_to,
                )
            if surface_type == "telegram":
                return self._publish_telegram(
                    sender=sender, text=text, reply_to=reply_to,
                )
            if surface_type == "twilio_sms":
                return self._publish_sms(
                    sender=sender, text=text, reply_to=reply_to,
                )
            logger.warning(
                "[outbound] no handler for surface=%r — dropping (sender=%s)",
                surface_type, sender,
            )
            return False
        except Exception:
            logger.warning(
                "[outbound] publish failed surface=%s sender=%s",
                surface_type, sender, exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # Per-surface handlers
    # ------------------------------------------------------------------

    def _publish_socketio(
        self,
        *,
        sender: str,
        text: str,
        reply_to: Dict[str, Any],
        request_id: Optional[str],
        sub_data_type: Optional[list[str]],
    ) -> bool:
        """UI socket — publish a UserMessage on the ``socket_emit`` topic.
        EmiEventRelay routes to the right room.
        """
        msg = UserMessage(
            data_type="user_msg",
            sub_data_type=list(sub_data_type or []),
            sender=sender,
            receiver=None,
            request_id=request_id,
            role="assistant",
            content=text,
            timestamp=datetime.now(timezone.utc),
            event_topic="socket_emit",
            metadata={"reply_to": dict(reply_to)},
            user_message_data=UserMessageData(chat=text),
        )
        DI.event_hub.publish(msg)
        logger.info(
            "[outbound] socketio publish sender=%r room=%s len=%d",
            sender, reply_to.get("room_id"), len(text),
        )
        return True

    def _publish_slack(
        self, *, sender: str, text: str, reply_to: Dict[str, Any],
    ) -> bool:
        """Slack — call SlackRoomTransport.send_reply directly.

        The transport lives on RoomSessionManager (the same path
        slack_inbound_service uses for replies). Worker name is
        embedded in the body since Slack doesn't render arbitrary
        per-message senders.
        """
        channel_id = str(reply_to.get("channel_id") or "").strip()
        thread_ts = str(reply_to.get("thread_ts") or "").strip()
        if not channel_id:
            logger.warning("[outbound] slack reply_to missing channel_id; dropping")
            return False
        rsm = getattr(DI, "room_session_manager", None)
        slack_transport = getattr(rsm, "slack_transport", None) if rsm else None
        if slack_transport is None:
            logger.warning("[outbound] slack_transport not available; dropping")
            return False
        body = self._embed_sender_in_body(sender, text)
        slack_transport.send_reply(
            channel_id=channel_id, body=body, thread_ts=thread_ts,
        )
        logger.info(
            "[outbound] slack publish sender=%r channel=%s len=%d",
            sender, channel_id, len(text),
        )
        return True

    def _publish_telegram(
        self, *, sender: str, text: str, reply_to: Dict[str, Any],
    ) -> bool:
        """Telegram — call TelegramRoomTransport.send_reply."""
        chat_id = str(reply_to.get("chat_id") or "").strip()
        if not chat_id:
            logger.warning("[outbound] telegram reply_to missing chat_id; dropping")
            return False
        rsm = getattr(DI, "room_session_manager", None)
        telegram_transport = getattr(rsm, "telegram_transport", None) if rsm else None
        if telegram_transport is None:
            logger.warning("[outbound] telegram_transport not available; dropping")
            return False
        body = self._embed_sender_in_body(sender, text)
        telegram_transport.send_reply(chat_id=chat_id, body=body)
        logger.info(
            "[outbound] telegram publish sender=%r chat=%s len=%d",
            sender, chat_id, len(text),
        )
        return True

    def _publish_sms(
        self, *, sender: str, text: str, reply_to: Dict[str, Any],
    ) -> bool:
        """SMS — call SmsRoomTransport.send_reply with the to/from numbers."""
        to_number = str(reply_to.get("to") or "").strip()
        from_number = str(reply_to.get("from") or "").strip()
        if not to_number:
            logger.warning("[outbound] sms reply_to missing 'to'; dropping")
            return False
        rsm = getattr(DI, "room_session_manager", None)
        sms_transport = getattr(rsm, "sms_transport", None) if rsm else None
        if sms_transport is None:
            logger.warning("[outbound] sms_transport not available; dropping")
            return False
        body = self._embed_sender_in_body(sender, text)
        # SmsRoomTransport.send_reply takes keyword-only (to_number, from_number, body).
        sms_transport.send_reply(
            to_number=to_number,
            from_number=from_number or "",
            body=body,
        )
        logger.info(
            "[outbound] sms publish sender=%r to=%s len=%d",
            sender, to_number, len(text),
        )
        return True

    @staticmethod
    def _embed_sender_in_body(sender: str, text: str) -> str:
        """Surfaces that don't render per-message senders (Slack, SMS,
        Telegram) need the worker's name embedded in the body so the
        user can tell who's speaking. Idempotent — won't double-prefix.
        """
        if not sender:
            return text
        prefix = f"[{sender}]"
        if text.startswith(prefix):
            return text
        return f"{prefix} {text}"
