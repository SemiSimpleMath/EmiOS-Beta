from __future__ import annotations

import uuid
from typing import Any, Dict

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.services.room_policy_service import (
    normalize_message_persistence_mode,
    resolve_room_unified_log_persistence,
)
from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class SmsInboundService:
    def handle(
        self,
        manager,
        *,
        from_number: str,
        to_number: str,
        body: str,
        message_sid: str,
        room_id: str,
        send_reply: bool = False,
        message_persistence_mode: str = "global_blackboard_and_unified_log",
    ) -> Dict[str, Any]:
        persistence_mode = normalize_message_persistence_mode(message_persistence_mode)
        room_contact_name = manager._derive_room_contact_name(room_id)
        room_inbound_timestamp_local = manager._local_timestamp_str()
        room_inbound_line = f"[{room_inbound_timestamp_local}] {room_contact_name}: {body}"
        request_id = str(uuid.uuid4())
        body, plan_meta, early = manager.ingress_service.apply_room_mode_context(
            room_id=room_id,
            surface="sms",
            context_id="main",
            body=str(body or ""),
            metadata={},
            sender_identity=from_number,
        )
        if isinstance(early, dict):
            reply_text = str(early.get("reply_text") or "").strip()
            outbound_sid = None
            if send_reply and reply_text:
                try:
                    outbound_sid = manager.sms_transport.send_reply(
                        to_number=from_number,
                        from_number=to_number,
                        body=reply_text,
                    )
                except Exception as e:
                    logger.error("Failed sending early plan-mode SMS reply for room_id=%s: %s", room_id, e)
                    logger.debug("early plan-mode SMS reply exception details", exc_info=True)
                    raise
            return manager._build_short_circuit_response(
                request_id=request_id,
                room_id=room_id,
                room_surface="sms",
                room_context_id="main",
                send_reply=send_reply,
                reply_text=reply_text,
                message_persistence_mode=persistence_mode,
                unified_log_persistence_reason="planning_mode_command",
                extra_fields={"outbound_message_sid": outbound_sid},
            )

        sms_reply_to = {
            "type": "twilio_sms",
            "to": from_number,
            "from": to_number,
            "room_id": room_id,
            "room_surface": "sms",
        }
        ask_user_result = manager.question_answer_service.try_resolve(
            request_id=request_id,
            room_id=room_id,
            room_surface="sms",
            room_context_id="main",
            body=body,
            send_reply=send_reply,
            message_persistence_mode=persistence_mode,
            include_dry_run=False,
            user_identity=from_number,
            reply_to=sms_reply_to,
            send_text=lambda text: manager.sms_transport.send_reply(
                to_number=from_number,
                from_number=to_number,
                body=text,
            ),
            outbound_result_key="outbound_message_sid",
            allow_binding_resolution=True,
            build_short_circuit_response=manager._build_short_circuit_response,
        )
        if isinstance(ask_user_result, dict):
            return ask_user_result

        metadata = {
            "reply_to": {
                "type": "twilio_sms",
                "to": from_number,
                "from": to_number,
                "inbound_message_sid": message_sid,
                "room_id": room_id,
                "room_surface": "sms",
            }
        }
        metadata.update(plan_meta)
        envelope = InboundEnvelope(
            surface="sms",
            room_id=room_id,
            context_id="main",
            request_id=request_id,
            speaker_name=room_contact_name,
            speaker_id=f"sms:{from_number}",
            speaker_external_id=from_number,
            content=body,
            timestamp_local=room_inbound_timestamp_local,
            inbound_line=room_inbound_line,
            transport_message_id=message_sid,
            transport_from=from_number,
            transport_to=to_number,
            reply_to=metadata.get("reply_to") if isinstance(metadata.get("reply_to"), dict) else {},
            metadata=metadata,
            extras={"from_number": from_number, "to_number": to_number, "message_sid": message_sid},
        )
        room_ctx = load_room_context_for_manager(room_id)
        persist_unified_log, persist_reason = resolve_room_unified_log_persistence(
            message_persistence_mode=persistence_mode,
            room_ctx=room_ctx if isinstance(room_ctx, dict) else {},
            room_id=room_id,
            room_surface="sms",
        )

        adapter = manager._build_sms_adapter(
            request_id=request_id,
            room_id=room_id,
            room_contact_name=room_contact_name,
            from_number=from_number,
            to_number=to_number,
            message_sid=message_sid,
            body=body,
            room_inbound_timestamp_local=room_inbound_timestamp_local,
        )
        return manager._handle_inbound_generic(
            envelope=envelope,
            room_ctx=room_ctx if isinstance(room_ctx, dict) else {},
            room_contact_name=room_contact_name,
            send_reply=send_reply,
            message_persistence_mode=persistence_mode,
            persist_unified_log=persist_unified_log,
            persist_reason=persist_reason,
            adapter=adapter,
        )
