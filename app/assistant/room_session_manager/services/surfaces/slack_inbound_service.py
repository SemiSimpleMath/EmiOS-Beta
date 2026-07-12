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


class SlackInboundService:
    @staticmethod
    def _compose_slack_content(*, body: str, image_paths: list[str] | None, allow_images: bool) -> str:
        text = str(body or "").strip()
        if not allow_images:
            return text
        paths = image_paths if isinstance(image_paths, list) else []
        markers = [f"[emi_image: {p}]" for p in paths if isinstance(p, str) and p.strip()]
        if markers:
            return "\n".join([text] + markers) if text else "\n".join(markers)
        return text

    def handle(
        self,
        manager,
        *,
        channel_id: str,
        body: str,
        room_id: str,
        message_ts: str = "",
        sender_name: str = "",
        sender_id: str = "",
        thread_ts: str = "",
        image_paths: list[str] | None = None,
        send_reply: bool = False,
        persist_to_history: bool = True,
        message_persistence_mode: str = "global_blackboard_only",
        simulated: bool = False,
    ) -> Dict[str, Any]:
        persistence_mode = normalize_message_persistence_mode(message_persistence_mode)
        resolved_request_id = str(uuid.uuid4())
        room_ctx = load_room_context_for_manager(room_id)
        # Prefer the contact name resolved from policy.json's participant_identity
        # (set in load_room_context_for_manager). Fall back to the derived-from-
        # room_id helper only if the policy didn't supply one. Matches the
        # Telegram inbound service's pattern.
        room_contact_name = (
            (room_ctx.get("room_contact_name") if isinstance(room_ctx, dict) else None)
            or manager._derive_room_contact_name(room_id)
            or "Slack"
        )
        persist_unified_log, persist_reason = resolve_room_unified_log_persistence(
            message_persistence_mode=persistence_mode,
            room_ctx=room_ctx if isinstance(room_ctx, dict) else {},
            room_id=room_id,
            room_surface="slack",
        )
        room_permissions = room_ctx.get("room_permissions") if isinstance(room_ctx, dict) else {}
        allow_images = bool(room_permissions.get("allow_images", False)) if isinstance(room_permissions, dict) else False
        inbound_content = self._compose_slack_content(body=body, image_paths=image_paths, allow_images=allow_images)
        slack_context_id = str(thread_ts or "main").strip() or "main"
        inbound_content, plan_meta, early = manager.ingress_service.apply_room_mode_context(
            room_id=room_id,
            surface="slack",
            context_id=slack_context_id,
            body=inbound_content,
            metadata={},
            sender_identity=sender_id or sender_name,
        )
        if isinstance(early, dict):
            reply_text = str(early.get("reply_text") or "").strip()
            outbound_status = None
            if send_reply and reply_text:
                try:
                    outbound_status = manager.slack_transport.send_reply(
                        channel_id=channel_id,
                        body=reply_text,
                        thread_ts=thread_ts or message_ts or "",
                    )
                except Exception as e:
                    # Log and swallow (match the Telegram early path): the reply is
                    # what failed, the inbound already returned 200, and the async
                    # worker that called us catches exceptions — re-raising only re-logs.
                    logger.error(
                        "Failed sending early plan-mode Slack reply for room_id=%s (logged, not re-raised): %s",
                        room_id, e,
                    )
                    logger.debug("early plan-mode Slack reply exception details", exc_info=True)
                    manager.surface_early_reply_failure(
                        room_id=room_id, surface="slack", reply_text=reply_text, error=e)
            return manager._build_short_circuit_response(
                request_id=resolved_request_id,
                room_id=room_id,
                room_surface="slack",
                room_context_id=slack_context_id,
                send_reply=send_reply,
                reply_text=reply_text,
                message_persistence_mode=persistence_mode,
                unified_log_persistence_reason="planning_mode_command",
                include_dry_run=True,
                extra_fields={"outbound_status": outbound_status},
            )

        slack_reply_to = {
            "type": "slack",
            "channel_id": channel_id,
            "thread_ts": thread_ts or message_ts or "",
            "room_id": room_id,
            "room_surface": "slack",
        }
        ask_user_result = manager.question_answer_service.try_resolve(
            request_id=resolved_request_id,
            room_id=room_id,
            room_surface="slack",
            room_context_id=slack_context_id,
            body=inbound_content,
            send_reply=send_reply,
            message_persistence_mode=persistence_mode,
            include_dry_run=True,
            user_identity=sender_id or sender_name,
            reply_to=slack_reply_to,
            send_text=lambda text: manager.slack_transport.send_reply(
                channel_id=channel_id,
                body=text,
                thread_ts=thread_ts or message_ts or "",
            ),
            outbound_result_key="outbound_status",
            allow_binding_resolution=True,
            build_short_circuit_response=manager._build_short_circuit_response,
        )
        if isinstance(ask_user_result, dict):
            return ask_user_result

        if not inbound_content.strip():
            return manager._build_short_circuit_response(
                request_id=resolved_request_id,
                room_id=room_id,
                room_surface="slack",
                room_context_id=slack_context_id,
                send_reply=send_reply,
                reply_text="",
                message_persistence_mode=persistence_mode,
                unified_log_persistence_reason="empty_inbound_content",
                include_dry_run=True,
                extra_fields={"outbound_status": None},
            )

        inbound_ts_local = manager._local_timestamp_str()
        inbound_line = f"[{inbound_ts_local}] {sender_name}: {inbound_content}"
        request_id = resolved_request_id

        metadata = {
            "reply_to": {
                "type": "slack",
                "channel_id": channel_id,
                "thread_ts": thread_ts or message_ts or "",
                "room_id": room_id,
                "room_surface": "slack",
            }
        }
        metadata.update(plan_meta)
        envelope = InboundEnvelope(
            surface="slack",
            room_id=room_id,
            context_id=slack_context_id,
            request_id=request_id,
            speaker_name=sender_name,
            speaker_id=f"slack:{sender_id or sender_name}",
            speaker_external_id=sender_id or None,
            content=inbound_content,
            timestamp_local=inbound_ts_local,
            inbound_line=inbound_line,
            transport_message_id=message_ts or "",
            transport_from=sender_id or sender_name,
            transport_to=channel_id,
            reply_to=metadata.get("reply_to") if isinstance(metadata.get("reply_to"), dict) else {},
            metadata=metadata,
            extras={
                "channel_id": channel_id,
                "message_ts": message_ts,
                "thread_ts": thread_ts or "",
                "sender_name": sender_name,
                "sender_id": sender_id or "",
                "allow_images": bool(allow_images),
                "room_simulated": bool(simulated),
            },
        )
        adapter = manager._build_slack_adapter(
            request_id=request_id,
            room_id=room_id,
            room_contact_name=room_contact_name,
            room_context_id=slack_context_id,
            sender_name=sender_name,
            sender_id=sender_id,
            inbound_content=inbound_content,
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            allow_images=allow_images,
            simulated=simulated,
            inbound_ts_local=inbound_ts_local,
            persist_to_history=persist_to_history,
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
