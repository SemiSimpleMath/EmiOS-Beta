from __future__ import annotations

import uuid
from typing import Any, Dict

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.services.room_policy_service import (
    normalize_message_persistence_mode,
    resolve_room_unified_log_persistence,
)
from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
from app.assistant.utils.identity_names import get_required_primary_user_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class UiInboundService:
    def handle(
        self,
        manager,
        *,
        socket_id: str,
        body: str,
        room_id: str,
        request_id: str = "",
        sender_name: str = "",
        send_reply: bool = True,
        message_persistence_mode: str = "global_blackboard_and_unified_log",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        persistence_mode = normalize_message_persistence_mode(message_persistence_mode)
        resolved_request_id = str(request_id or "").strip() or str(uuid.uuid4())
        primary_user_name = get_required_primary_user_name()
        resolved_sender_name = str(sender_name or "").strip() or primary_user_name
        room_contact_name = primary_user_name
        room_ctx = load_room_context_for_manager(room_id)
        persist_unified_log, persist_reason = resolve_room_unified_log_persistence(
            message_persistence_mode=persistence_mode,
            room_ctx=room_ctx if isinstance(room_ctx, dict) else {},
            room_id=room_id,
            room_surface="ui",
        )
        content = str(body or "").strip()
        base_meta = dict(metadata) if isinstance(metadata, dict) else {}
        room_context_id = str(base_meta.get("room_context_id") or "main").strip() or "main"
        content, base_meta, early = manager.ingress_service.apply_room_mode_context(
            room_id=room_id,
            surface="ui",
            context_id=room_context_id,
            body=content,
            metadata=base_meta,
            sender_identity=resolved_sender_name or "ui:user",
        )
        logger.info(
            "[ui_inbound] after apply_room_mode_context: room_mode=%s, has_early=%s, content=%s",
            base_meta.get("room_mode", "(missing)"),
            isinstance(early, dict),
            (content or "")[:40],
        )
        if isinstance(early, dict):
            reply_text = str(early.get("reply_text") or "").strip()
            widget_data = early.get("widget_data")
            if send_reply and (reply_text or widget_data):
                try:
                    manager.ui_transport.send_reply(
                        room_id=room_id,
                        body=reply_text,
                        request_id=resolved_request_id,
                        metadata=base_meta,
                        widget_data=widget_data if isinstance(widget_data, list) else None,
                    )
                except Exception as e:
                    logger.error("Failed sending early plan-mode UI reply for room_id=%s: %s", room_id, e)
                    logger.debug("early plan-mode UI reply exception details", exc_info=True)
                    raise
            return manager._build_short_circuit_response(
                request_id=resolved_request_id,
                room_id=room_id,
                room_surface="ui",
                room_context_id=room_context_id,
                send_reply=send_reply,
                reply_text=reply_text,
                message_persistence_mode=persistence_mode,
                unified_log_persistence_reason="early_return_no_persistence",
                reply_to={"type": "socketio", "room_id": room_id},
                include_reply_text_when_not_sent=True,
            )
        atts = base_meta.get("attachments") if isinstance(base_meta, dict) else None
        if isinstance(atts, list) and atts:
            image_markers: list[str] = []
            for att in atts:
                if not isinstance(att, dict) or att.get("type") != "image":
                    continue
                # Prefer the pod URI when an image pod was minted at upload
                # time. The naked datapod:image:... URI lets the entity
                # resolver, fact_extractor, and PodInjector pick it up
                # uniformly — no marker syntax to teach each agent. Fall
                # back to the legacy path marker only when ingest_image_file
                # failed (process_request.py logs a warning in that case).
                pod_id = str(att.get("pod_id") or "").strip()
                if pod_id:
                    image_markers.append(pod_id)
                    continue
                path = str(att.get("path") or "").strip()
                if not path:
                    continue
                image_markers.append(f"[emi_image: {path}]")
            if image_markers:
                marker_block = "\n".join(image_markers)
                content = f"{content}\n{marker_block}" if content else marker_block
        if not content:
            raise ValueError("UI inbound body cannot be empty.")

        inbound_ts_local = manager._local_timestamp_str()
        inbound_line = f"[{inbound_ts_local}] {resolved_sender_name}: {content}"
        request_id = resolved_request_id
        reply_to: Dict[str, Any] = {"type": "socketio", "room_id": room_id}
        # Preserve TTS/interaction-mode flags that were injected into metadata by the caller.
        inbound_tts = base_meta.get("reply_to", {}).get("tts_requested") if isinstance(base_meta.get("reply_to"), dict) else None
        if inbound_tts is None:
            inbound_tts = base_meta.get("tts_requested")
        if inbound_tts is not None:
            reply_to["tts_requested"] = bool(inbound_tts)
        logger.info("[TTS:ui_ingress] room_id=%s tts_requested=%s (inbound_tts=%r)", room_id, reply_to.get("tts_requested"), inbound_tts)
        route_meta = {**base_meta, "reply_to": reply_to}
        # Ensure every message has a room_mode. Without this, messages
        # persisted in normal mode are indistinguishable from old untagged
        # task_creation messages, and the history filter can't exclude them.
        route_meta.setdefault("room_mode", "normal")
        logger.info(
            "[ui_inbound] route_meta built: room_mode=%s, keys=%s",
            route_meta.get("room_mode", "(missing)"),
            [k for k in route_meta.keys() if k != "reply_to"],
        )

        envelope = InboundEnvelope(
            surface="ui",
            room_id=room_id,
            context_id=room_context_id,
            request_id=request_id,
            speaker_name=resolved_sender_name,
            speaker_id="ui:user",
            speaker_external_id=resolved_sender_name or None,
            content=content,
            timestamp_local=inbound_ts_local,
            inbound_line=inbound_line,
            transport_message_id="",
            transport_from=resolved_sender_name,
            transport_to=str(socket_id or "").strip(),
            reply_to=reply_to,
            metadata=route_meta,
            extras={"socket_id": str(socket_id or "").strip(), "sender_name": resolved_sender_name},
        )
        adapter = manager._build_ui_adapter(
            request_id=request_id,
            room_id=room_id,
            room_contact_name=room_contact_name,
            room_context_id=room_context_id,
            sender_name=resolved_sender_name,
            content=content,
            socket_id=str(socket_id or "").strip(),
            route_meta=route_meta,
            inbound_ts_local=inbound_ts_local,
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
