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


class TelegramInboundService:
    def handle(
        self,
        manager,
        *,
        chat_id: str,
        body: str,
        room_id: str,
        message_id: str = "",
        sender_name: str = "",
        sender_id: str = "",
        send_reply: bool = False,
        message_persistence_mode: str = "global_blackboard_and_unified_log",
    ) -> Dict[str, Any]:
        persistence_mode = normalize_message_persistence_mode(message_persistence_mode)
        request_id = str(uuid.uuid4())
        room_ctx = load_room_context_for_manager(room_id)
        # Prefer display_name from room policy (e.g. "Katy"), fall back to room_id derivation.
        room_contact_name = (
            (room_ctx.get("room_contact_name") if isinstance(room_ctx, dict) else None)
            or manager._derive_room_contact_name(room_id)
            or "Telegram"
        )
        persist_unified_log, persist_reason = resolve_room_unified_log_persistence(
            message_persistence_mode=persistence_mode,
            room_ctx=room_ctx if isinstance(room_ctx, dict) else {},
            room_id=room_id,
            room_surface="telegram",
        )
        body, plan_meta, early = manager.ingress_service.apply_room_mode_context(
            room_id=room_id,
            surface="telegram",
            context_id="main",
            body=str(body or ""),
            metadata={},
            sender_identity=sender_id or sender_name or chat_id,
        )
        if isinstance(early, dict):
            reply_text = str(early.get("reply_text") or "").strip()
            outbound_message_id = None
            if send_reply and reply_text:
                try:
                    outbound_message_id = manager.telegram_transport.send_reply(chat_id=chat_id, body=reply_text)
                except Exception as e:
                    # Log and swallow: the reply is what failed and the early-return
                    # path has nothing left to do. The inbound webhook already
                    # returned 200 on its own thread, and the async worker that
                    # called us catches exceptions anyway — re-raising would only
                    # re-log. NOTE: this early path does NOT persist to unified_log
                    # (the short-circuit response reports
                    # unified_log_persistence_enabled=False); a slash/plan-mode
                    # control reply is not recorded as chat history.
                    logger.error(
                        "Failed sending early plan-mode Telegram reply for room_id=%s (logged, not re-raised): %s",
                        room_id, e,
                    )
                    logger.debug("early plan-mode telegram reply exception details", exc_info=True)
                    manager.surface_early_reply_failure(
                        room_id=room_id, surface="telegram", reply_text=reply_text, error=e)
            return manager._build_short_circuit_response(
                request_id=request_id,
                room_id=room_id,
                room_surface="telegram",
                room_context_id="main",
                send_reply=send_reply,
                reply_text=reply_text,
                message_persistence_mode=persistence_mode,
                unified_log_persistence_reason="planning_mode_command",
                extra_fields={"outbound_message_id": outbound_message_id},
            )

        telegram_reply_to = {
            "type": "telegram",
            "chat_id": chat_id,
            "room_id": room_id,
            "room_surface": "telegram",
        }
        ask_user_result = manager.question_answer_service.try_resolve(
            request_id=request_id,
            room_id=room_id,
            room_surface="telegram",
            room_context_id="main",
            body=body,
            send_reply=send_reply,
            message_persistence_mode=persistence_mode,
            include_dry_run=False,
            user_identity=sender_id or sender_name or chat_id,
            reply_to=telegram_reply_to,
            send_text=lambda text: manager.telegram_transport.send_reply(chat_id=chat_id, body=text),
            outbound_result_key="outbound_message_id",
            allow_binding_resolution=False,
            build_short_circuit_response=manager._build_short_circuit_response,
        )
        if isinstance(ask_user_result, dict):
            return ask_user_result

        metadata = {
            "reply_to": {
                "type": "telegram",
                "chat_id": chat_id,
                "room_id": room_id,
                "room_surface": "telegram",
            }
        }
        telegram_ts_local = manager._local_timestamp_str()
        metadata.update(plan_meta)
        envelope = InboundEnvelope(
            surface="telegram",
            room_id=room_id,
            context_id="main",
            request_id=request_id,
            speaker_name=sender_name or room_contact_name,
            speaker_id=f"telegram:{sender_id}" if sender_id else f"telegram:{chat_id}",
            speaker_external_id=sender_id or chat_id,
            content=body,
            timestamp_local=telegram_ts_local,
            inbound_line=f"[{telegram_ts_local}] {sender_name or room_contact_name}: {body}",
            transport_message_id=str(message_id or ""),
            transport_from=sender_id or chat_id,
            transport_to=chat_id,
            reply_to=metadata.get("reply_to") if isinstance(metadata.get("reply_to"), dict) else {},
            metadata=metadata,
            extras={"chat_id": chat_id, "sender_id": sender_id, "sender_name": sender_name, "message_id": message_id},
        )

        adapter = manager._build_telegram_adapter(
            request_id=request_id,
            room_id=room_id,
            room_contact_name=room_contact_name,
            chat_id=chat_id,
            message_id=str(message_id or ""),
            sender_name=sender_name,
            sender_id=sender_id,
            body=body,
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
