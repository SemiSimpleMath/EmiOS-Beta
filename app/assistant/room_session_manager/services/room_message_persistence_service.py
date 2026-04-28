from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.identity_names import (
    get_required_assistant_name,
    get_required_primary_user_name,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class RoomMessagePersistenceService:
    def __init__(self, *, blackboard: GlobalBlackBoard) -> None:
        self._blackboard = blackboard
    def append_sms_inbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        from_number: str,
        to_number: str,
        message_sid: str,
        body: str,
        room_inbound_timestamp_local: str,
    ) -> Message | None:
        try:
            inbound_msg = Message(
                data_type="user_msg",
                sender=room_contact_name,
                role="user",
                content=body,
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="sms",
                room_context_id="main",
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="inbound",
                room_initiated_by="user",
                room_delivery_mode="auto_send",
                room_speaker_id=f"sms:{from_number}",
                room_speaker_name=room_contact_name,
                room_speaker_role="participant",
                room_speaker_external_id=from_number,
                room_actor_id=f"sms:{from_number}",
                transport_message_id=message_sid,
                transport_from=from_number,
                transport_to=to_number,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_inbound_timestamp_local": room_inbound_timestamp_local,
                    "room_surface": "sms",
                    "from_number": from_number,
                    "to_number": to_number,
                    "message_sid": message_sid,
                },
            )
            self._blackboard.add_msg(inbound_msg)
            return inbound_msg
        except Exception as e:
            logger.error("Failed appending inbound room message to session history: %s", e)
            logger.debug("sms inbound append exception details", exc_info=True)
            return None

    def append_sms_outbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        from_number: str,
        to_number: str,
        reply_text: str,
        twilio_sid: str | None = None,
    ) -> Message | None:
        if not reply_text.strip():
            return None
        try:
            assistant_name = get_required_assistant_name()
            outbound_msg = Message(
                data_type="user_msg",
                sender=assistant_name,
                role="assistant",
                content=reply_text.strip(),
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="sms",
                room_context_id="main",
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="outbound",
                room_initiated_by="agent",
                room_delivery_mode="auto_send",
                room_speaker_id=f"assistant:{assistant_name.lower()}",
                room_speaker_name=assistant_name,
                room_speaker_role="assistant",
                room_actor_id=f"assistant:{assistant_name.lower()}",
                transport_message_id=twilio_sid,
                transport_from=to_number,
                transport_to=from_number,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "sms",
                    "from_number": to_number,
                    "to_number": from_number,
                },
            )
            self._blackboard.add_msg(outbound_msg)
            return outbound_msg
        except Exception as e:
            logger.error("Failed appending outbound room message to session history: %s", e)
            logger.debug("sms outbound append exception details", exc_info=True)
            return None

    def append_telegram_inbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        chat_id: str,
        message_id: str,
        sender_name: str,
        sender_id: str,
        body: str,
    ) -> Message | None:
        try:
            inbound_msg = Message(
                data_type="user_msg",
                sender=sender_name or room_contact_name,
                role="user",
                content=body,
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="telegram",
                room_context_id="main",
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="inbound",
                room_initiated_by="user",
                room_delivery_mode="auto_send",
                room_speaker_id=f"telegram:{sender_id}" if sender_id else f"telegram:{chat_id}",
                room_speaker_name=sender_name or room_contact_name,
                room_speaker_role="participant",
                room_speaker_external_id=sender_id or chat_id,
                room_actor_id=f"telegram:{sender_id}" if sender_id else f"telegram:{chat_id}",
                transport_message_id=message_id,
                transport_from=sender_id or chat_id,
                transport_to=chat_id,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "telegram",
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "message_id": message_id,
                },
            )
            self._blackboard.add_msg(inbound_msg)
            return inbound_msg
        except Exception as e:
            logger.error("Failed appending inbound Telegram room message to session history: %s", e)
            logger.debug("telegram inbound append exception details", exc_info=True)
            return None

    def append_telegram_outbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        chat_id: str,
        reply_text: str,
        outbound_message_id: str | None = None,
    ) -> Message | None:
        if not reply_text.strip():
            return None
        try:
            assistant_name = get_required_assistant_name()
            outbound_msg = Message(
                data_type="user_msg",
                sender=assistant_name,
                role="assistant",
                content=reply_text.strip(),
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="telegram",
                room_context_id="main",
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="outbound",
                room_initiated_by="agent",
                room_delivery_mode="auto_send",
                room_speaker_id=f"assistant:{assistant_name.lower()}",
                room_speaker_name=assistant_name,
                room_speaker_role="assistant",
                room_actor_id=f"assistant:{assistant_name.lower()}",
                transport_message_id=outbound_message_id,
                transport_from=f"{assistant_name.lower()}_bot",
                transport_to=chat_id,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "telegram",
                    "chat_id": chat_id,
                },
            )
            self._blackboard.add_msg(outbound_msg)
            return outbound_msg
        except Exception as e:
            logger.error("Failed appending outbound Telegram room message to session history: %s", e)
            logger.debug("telegram outbound append exception details", exc_info=True)
            return None

    def append_ui_inbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        room_context_id: str,
        sender_name: str,
        content: str,
        socket_id: str,
        route_meta: Dict[str, Any],
        inbound_ts_local: str,
    ) -> Message | None:
        try:
            user_name = (
                sender_name.strip()
                if isinstance(sender_name, str) and sender_name.strip()
                else get_required_primary_user_name()
            )
            inbound_msg = Message(
                data_type="user_msg",
                sender=user_name,
                role="user",
                content=content,
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="ui",
                room_context_id=room_context_id,
                room_visibility="owner_only",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="inbound",
                room_initiated_by="user",
                room_delivery_mode="auto_send",
                room_speaker_id="ui:user",
                room_speaker_name=user_name,
                room_speaker_role="owner",
                room_speaker_external_id=user_name or None,
                room_actor_id="ui:user",
                transport_message_id=None,
                transport_from=user_name,
                transport_to=socket_id,
                metadata=route_meta,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "ui",
                    "socket_id": socket_id,
                    "sender_name": user_name,
                    "room_inbound_timestamp_local": inbound_ts_local,
                },
            )
            self._blackboard.add_msg(inbound_msg)
            return inbound_msg
        except Exception as e:
            logger.error("Failed appending inbound UI room message to session history: %s", e)
            logger.debug("ui inbound append exception details", exc_info=True)
            return None

    def append_ui_outbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        room_context_id: str,
        reply_text: str,
        socket_id: str,
        route_meta: Dict[str, Any],
    ) -> Message | None:
        if not reply_text.strip():
            return None
        try:
            assistant_name = get_required_assistant_name()
            outbound_msg = Message(
                data_type="user_msg",
                sender=assistant_name,
                role="assistant",
                content=reply_text.strip(),
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="ui",
                room_context_id=room_context_id,
                room_visibility="owner_only",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="outbound",
                room_initiated_by="agent",
                room_delivery_mode="auto_send",
                room_speaker_id=f"assistant:{assistant_name.lower()}",
                room_speaker_name=assistant_name,
                room_speaker_role="assistant",
                room_actor_id=f"assistant:{assistant_name.lower()}",
                transport_message_id=None,
                transport_from=f"assistant:{assistant_name.lower()}",
                transport_to=socket_id,
                metadata=route_meta,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "ui",
                    "socket_id": socket_id,
                },
            )
            self._blackboard.add_msg(outbound_msg)
            return outbound_msg
        except Exception as e:
            logger.error("Failed appending outbound UI room message to session history: %s", e)
            logger.debug("ui outbound append exception details", exc_info=True)
            return None

    def append_slack_inbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        room_context_id: str,
        sender_name: str,
        sender_id: str,
        content: str,
        channel_id: str,
        message_ts: str,
        thread_ts: str,
        allow_images: bool,
        simulated: bool,
        inbound_ts_local: str,
    ) -> Message | None:
        try:
            inbound_msg = Message(
                data_type="user_msg",
                sender=sender_name,
                role="user",
                content=content,
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="slack",
                room_context_id=room_context_id,
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="inbound",
                room_initiated_by="user",
                room_delivery_mode="auto_send",
                room_speaker_id=f"slack:{sender_id or sender_name}",
                room_speaker_name=sender_name,
                room_speaker_role="participant",
                room_speaker_external_id=sender_id or None,
                room_actor_id=f"slack:{sender_id or sender_name}",
                transport_message_id=message_ts or None,
                transport_from=sender_id or sender_name,
                transport_to=channel_id,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_inbound_timestamp_local": inbound_ts_local,
                    "room_surface": "slack",
                    "channel_id": channel_id,
                    "message_ts": message_ts,
                    "thread_ts": thread_ts or "",
                    "sender_name": sender_name,
                    "sender_id": sender_id or "",
                    "room_allow_images": allow_images,
                    "room_simulated": bool(simulated),
                },
            )
            self._blackboard.add_msg(inbound_msg)
            return inbound_msg
        except Exception as e:
            logger.error("Failed appending inbound Slack message to session history: %s", e)
            logger.debug("slack inbound append exception details", exc_info=True)
            return None

    def append_slack_outbound(
        self,
        *,
        request_id: str,
        room_id: str,
        room_contact_name: str,
        room_context_id: str,
        reply_text: str,
        channel_id: str,
        thread_ts: str,
        simulated: bool,
    ) -> Message | None:
        if not reply_text.strip():
            return None
        try:
            assistant_name = get_required_assistant_name()
            outbound_msg = Message(
                data_type="user_msg",
                sender=assistant_name,
                role="assistant",
                content=reply_text.strip(),
                is_chat=True,
                request_id=request_id,
                timestamp=datetime.now(timezone.utc),
                room_id=room_id,
                room_surface="slack",
                room_context_id=room_context_id,
                room_visibility="room_shared",
                room_policy_id=f"room_policy::{room_id}::v1",
                room_message_direction="outbound",
                room_initiated_by="agent",
                room_delivery_mode="auto_send",
                room_speaker_id=f"assistant:{assistant_name.lower()}",
                room_speaker_name=assistant_name,
                room_speaker_role="assistant",
                room_actor_id=f"assistant:{assistant_name.lower()}",
                transport_message_id=None,
                transport_from=f"assistant:{assistant_name.lower()}",
                transport_to=channel_id,
                data={
                    "room_id": room_id,
                    "room_contact_name": room_contact_name,
                    "room_surface": "slack",
                    "channel_id": channel_id,
                    "thread_ts": thread_ts or "",
                    "room_simulated": bool(simulated),
                },
            )
            self._blackboard.add_msg(outbound_msg)
            return outbound_msg
        except Exception as e:
            logger.error("Failed appending outbound Slack message to session history: %s", e)
            logger.debug("slack outbound append exception details", exc_info=True)
            return None
