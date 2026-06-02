from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Callable, Dict

from app.assistant.message_manager.save_to_unified_db import save_to_unified_db
from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.services import (
    PostRoomService,
    QuestionAnswerIngressService,
    RoomMessagePersistenceService,
    RoomHistoryBuilder,
    RoomIngressService,
    RoomSurfaceHandlerFactory,
    SlackRoomTransport,
    SmsRoomTransport,
    TelegramRoomTransport,
    UiRoomTransport,
)
from app.assistant.room_session_manager.services.room_policy_service import (
    resolve_room_manager_name,
    resolve_room_unified_log_persistence,
)
from app.assistant.room_session_manager.services.room_metadata_repair_service import (
    ensure_request_room_metadata,
)
from app.assistant.room_session_manager.services.room_resource_injection_service import (
    RoomResourceInjectionService,
)
from app.assistant.room_session_manager.services.room_scope_builder import (
    build_scope_contract_for_room_request,
)
from app.assistant.room_session_manager.services.room_mode_exit_service import (
    maybe_write_mode_exit_summary,
)
from app.assistant.dayflow_orchestrator.dayflow_tick import block_dayflow_orchestrator_for_master_chat
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.chat_formatting import messages_to_chat_excerpts
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


@dataclass(frozen=True)
class InboundSurfaceAdapter:
    inbound_source: str
    outbound_source: str
    append_inbound: Callable[[], Message | None]
    append_outbound: Callable[[str, Any], Message | None]
    send_outbound: Callable[[str], Any]
    outbound_result_key: str | None = None
    include_dry_run: bool = False
    persist_to_history: bool = True


class RoomSessionManager:
    """
    Transport-facing room session manager.

    Responsibilities:
    - Build a room-scoped inbound Message.
    - Invoke room-selected manager (default `room_manager`).
    - Persist room chat turns into in-memory global session history.
    - Optionally deliver outbound reply via transport.

    Persistence layers:
    - `persist_to_history`: write turn messages to global blackboard history.
    - `persist_unified_log`: write turn messages to unified log storage.
    - `send_reply`: deliver outbound text over the room transport.
    """

    def __init__(
            self,
            *,
            blackboard: Any = None,
            event_hub: Any = None,
            reply_router: Any = None,
            resource_manager: Any = None,
            manager_registry: Any = None,
            multi_agent_manager_factory: Any = None,
            manager_invoker: Any = None,
    ):
        from app.assistant.room_session_manager.services.plan_session_service import PlanSessionService
        from app.assistant.room_session_manager.services.task_creation_session_service import TaskCreationSessionService
        from app.assistant.room_session_manager.services.doc_creation_session_service import DocCreationSessionService
        from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
        from app.assistant.room_session_manager.services.actas_session_service import ActAsSessionService

        self._blackboard = blackboard
        self._reply_router = reply_router

        self.history_builder = RoomHistoryBuilder()
        self.post_room_service = PostRoomService()
        self.question_answer_service = QuestionAnswerIngressService()
        self.message_persistence_service = RoomMessagePersistenceService(
            blackboard=blackboard
        )
        self.ingress_service = RoomIngressService(
            plan_session_service=PlanSessionService(blackboard=blackboard) if blackboard is not None else None,
            task_creation_session_service=TaskCreationSessionService(blackboard=blackboard) if blackboard is not None else None,
            doc_creation_session_service=DocCreationSessionService(blackboard=blackboard) if blackboard is not None else None,
            geo_session_service=GeoguessrSessionService(blackboard=blackboard) if blackboard is not None else None,
            actas_session_service=ActAsSessionService(blackboard=blackboard) if blackboard is not None else None,
            manager_registry=manager_registry,
            multi_agent_manager_factory=multi_agent_manager_factory,
            manager_invoker=manager_invoker,
        )
        self.resource_injection_service = RoomResourceInjectionService(resource_manager=resource_manager)
        self.surface_handler_factory = RoomSurfaceHandlerFactory()
        self.sms_transport = SmsRoomTransport()
        self.slack_transport = SlackRoomTransport()
        self.telegram_transport = TelegramRoomTransport()
        self.ui_transport = UiRoomTransport(event_hub=event_hub) if event_hub is not None else None

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_room_contact_name(room_id: str) -> str:
        rid = str(room_id or "").strip()
        if not rid:
            return "User"
        cleaned = re.sub(r"[_\\-]+", " ", rid).strip()
        if not cleaned:
            return "User"
        return cleaned[:1].upper() + cleaned[1:]

    @staticmethod
    def _local_timestamp_str() -> str:
        return get_local_time().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _to_utc_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        raise TypeError(f"Unsupported timestamp type for room seeded history: {type(value).__name__}")

    # ------------------------------------------------------------------
    # Unified-log persistence
    # ------------------------------------------------------------------

    def _persist_message_to_unified_log(self, *, message: Message, source: str) -> None:
        try:
            msg_meta = message.metadata if isinstance(message.metadata, dict) else {}
            logger.info(
                "_persist_message_to_unified_log: source=%s room_id=%s room_mode=%s content=%s",
                source,
                message.room_id,
                msg_meta.get("room_mode", "(missing)"),
                (message.content or "")[:40],
            )
            payload = {
                "id": message.id,
                "timestamp": message.timestamp,
                "role": message.role or "unknown",
                "message": message.content or "",
                "request_id": message.request_id,
                "room_id": message.room_id,
                "room_surface": message.room_surface,
                "room_context_id": message.room_context_id,
                "direction": message.room_message_direction,
                "speaker_id": message.room_speaker_id,
                "speaker_name": message.room_speaker_name,
                "speaker_role": message.room_speaker_role,
                "speaker_external_id": message.room_speaker_external_id,
                "transport_message_id": message.transport_message_id,
                "transport_from": message.transport_from,
                "transport_to": message.transport_to,
                "metadata_json": message.metadata if isinstance(message.metadata, dict) else {},
                "data": message.data if isinstance(message.data, dict) else {},
                "sub_data_type": message.sub_data_type if isinstance(message.sub_data_type, list) else [],
                "test_mode": bool(getattr(message, "test_mode", False)),
            }
            save_to_unified_db([payload], source=source)
        except Exception as e:
            logger.error("Failed persisting room message to unified log (source=%s): %s", source, e)
            logger.debug("room unified-log persist exception details", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # Seeded history building
    # ------------------------------------------------------------------

    def _build_room_session_seeded_messages(
            self,
            *,
            room_id: str,
            limit: int | None = None,
            room_surface: str | None = None,
            room_context_id: str | None = None,
            shared_chat_room_ids: list[str] | None = None,
            current_room_mode: str | None = None,
            current_mode_session_id: str | None = None,
            history_policy: dict | None = None,
    ) -> list[dict[str, Any]]:
        from app.assistant.utils.message_visibility_policy import _NON_NORMAL_ROOM_MODES
        _MODE_TAGGED = _NON_NORMAL_ROOM_MODES
        _MODE_SESSION_KEYS = {
            "planning_mode": "plan_session_id",
            "task_creation_mode": "task_creation_session_id",
            "doc_creation_mode": "doc_creation_session_id",
            "game_mode": "geo_session_id",
        }
        _current = (current_room_mode or "normal").strip().lower()
        if _current == "normal":
            excluded = _MODE_TAGGED
        else:
            excluded = _MODE_TAGGED - {_current}

        seeded = self.history_builder.build_messages(
            room_id=room_id,
            limit=limit,
            room_surface=room_surface,
            room_context_id=room_context_id,
            shared_chat_room_ids=shared_chat_room_ids or [],
            excluded_room_modes=excluded,
        )
        logger.info(
            "[HISTORY_DIAG:B1] history_builder.build_messages returned %d msgs (room_id=%s mode=%s excluded=%s)",
            len(seeded), room_id, _current, sorted(excluded),
        )

        # Apply history policy from room policy.json.
        hp = history_policy if isinstance(history_policy, dict) else {}
        scope = str(hp.get("scope") or "").strip().lower()

        if scope == "time_bounded":
            max_hours = int(hp.get("max_hours", 24) or 24)
            cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=max_hours)
            seeded = [
                msg for msg in seeded
                if self._to_utc_timestamp(getattr(msg, "timestamp", None)) >= cutoff_utc
            ]
            logger.info("[HISTORY_DIAG:B2] after time_bounded filter: %d msgs", len(seeded))
        elif scope == "session":
            # Session-scoped: only include messages that have the current
            # session id, or messages from the last 2 minutes (current turn).
            # This gives a clean slate per session.
            session_key = _MODE_SESSION_KEYS.get(_current, "")
            session_id = current_mode_session_id
            # For standalone rooms (mode=normal), check if metadata has any session id.
            recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
            filtered = []
            for msg in seeded:
                meta = getattr(msg, "metadata", None)
                if not isinstance(meta, dict):
                    meta = {}
                # Include if message is from current turn (very recent).
                ts = self._to_utc_timestamp(getattr(msg, "timestamp", None))
                if ts and ts >= recent_cutoff:
                    filtered.append(msg)
                    continue
                # Include if message has matching session id.
                if session_key and session_id:
                    msg_sid = str(meta.get(session_key) or "").strip()
                    if msg_sid == session_id:
                        filtered.append(msg)
            logger.info(
                "[HISTORY_DIAG:B2] after session-scope filter: %d msgs (was %d, session_key=%s session_id=%s)",
                len(filtered), len(seeded), session_key, session_id,
            )
            seeded = filtered

        if _current in _MODE_TAGGED:
            expected_session_key = _MODE_SESSION_KEYS.get(_current, "")
            expected_session_id = str(current_mode_session_id or "").strip()
            filtered_seeded = []
            for msg in seeded:
                meta = getattr(msg, "metadata", None)
                if not isinstance(meta, dict):
                    continue
                msg_mode = str(meta.get("room_mode") or "").strip().lower()
                if msg_mode != _current:
                    continue
                if expected_session_key and expected_session_id:
                    msg_sid = str(meta.get(expected_session_key) or "").strip()
                    if msg_sid != expected_session_id:
                        continue
                filtered_seeded.append(msg)
            logger.info(
                "[HISTORY_DIAG:B3] after MODE_TAGGED filter: %d msgs (was %d, current=%s expected_session_id=%s)",
                len(filtered_seeded), len(seeded), _current, expected_session_id,
            )
            seeded = filtered_seeded

        out: list[dict[str, Any]] = []
        for msg in seeded:
            if hasattr(msg, "model_dump"):
                payload = msg.model_dump()
            elif hasattr(msg, "dict"):
                payload = msg.dict()
            else:
                raise TypeError(f"room seeded message must be Message-like, got {type(msg)}")
            try:
                excerpts = messages_to_chat_excerpts([msg], consumer_room_id=room_id)
                if excerpts:
                    excerpt = excerpts[0]
                    content = excerpt.get("content")
                    sender = excerpt.get("sender")
                    if isinstance(content, str) and content.strip():
                        payload["content"] = content
                    if isinstance(sender, str) and sender.strip():
                        payload["sender"] = sender
            except Exception as e:
                logger.error("Failed applying cross-room excerpt formatting to seeded message: %s", e)
                logger.debug("seeded message cross-room formatting exception details", exc_info=True)
                raise
            metadata = payload.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise ValueError("room seeded message metadata must be an object when provided.")
            metadata = dict(metadata)
            metadata["seeded_to_local_blackboard"] = True
            metadata["seed_source"] = "room_ingress_scoped_history"
            try:
                origin_room = str(getattr(msg, "room_id", "") or "").strip()
                consumer_room = str(room_id or "").strip()
                if origin_room and consumer_room and origin_room != consumer_room:
                    metadata["cross_room_tf"] = True
                    metadata["origin_room_id"] = origin_room
            except Exception as e:
                logger.error("Failed deriving cross-room metadata for seeded message: %s", e)
                logger.debug("seeded message cross-room metadata exception details", exc_info=True)
                raise
            payload["metadata"] = metadata
            out.append(payload)
        return out

    # ------------------------------------------------------------------
    # Short-circuit response builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_short_circuit_response(
            *,
            request_id: str,
            room_id: str,
            room_surface: str,
            room_context_id: str,
            send_reply: bool,
            reply_text: str,
            message_persistence_mode: str,
            unified_log_persistence_reason: str,
            reply_to: Dict[str, Any] | None = None,
            include_reply_text_when_not_sent: bool = False,
            include_dry_run: bool = False,
            extra_fields: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        text = str(reply_text or "").strip()
        should_expose = bool(include_reply_text_when_not_sent or send_reply)
        exposed_text = text if should_expose else ""
        payload = {"chat": text} if should_expose and text else {}
        result: Dict[str, Any] = {
            "request_id": request_id,
            "room_id": room_id,
            "reply_text": exposed_text,
            "reply_payload": payload,
            "outbound_intent": {
                "send": bool(send_reply and text),
                "reply_text": exposed_text,
                "reply_payload": payload,
                "request_id": request_id,
                "room_id": room_id,
                "room_surface": room_surface,
                "room_context_id": room_context_id,
                "reply_to": reply_to if isinstance(reply_to, dict) else {},
            },
            "message_persistence_mode": message_persistence_mode,
            "unified_log_persistence_enabled": False,
            "unified_log_persistence_reason": unified_log_persistence_reason,
        }
        if include_dry_run and not send_reply:
            result["dry_run"] = True
        if isinstance(extra_fields, dict) and extra_fields:
            result.update(extra_fields)
        return result

    # ------------------------------------------------------------------
    # Room handler resolution
    # ------------------------------------------------------------------

    _room_handler_cache: dict = {}

    def _get_room_handler(self, room_ctx: Dict[str, Any] | None):
        """Resolve a room-specific handler from the room's policy.json.

        Returns an EditorRoom instance (or subclass), or None if the room
        has no custom handler.
        """
        if not isinstance(room_ctx, dict):
            return None
        policy = room_ctx.get("room_policy")
        if not isinstance(policy, dict):
            return None
        handler_ref = str(policy.get("room_handler") or "").strip()
        if not handler_ref:
            return None

        room_id = str(room_ctx.get("room_id") or "").strip()
        cache_key = f"{room_id}::{handler_ref}"
        if cache_key in self._room_handler_cache:
            return self._room_handler_cache[cache_key]

        try:
            # handler_ref is "module_name.ClassName" under app.assistant.room_handlers
            parts = handler_ref.rsplit(".", 1)
            if len(parts) != 2:
                logger.error("Invalid room_handler format: %r (expected 'module.Class')", handler_ref)
                return None
            module_name, class_name = parts
            import importlib
            mod = importlib.import_module(f"app.assistant.room_handlers.{module_name}")
            cls = getattr(mod, class_name)
            instance = cls(room_id=room_id)
            self._room_handler_cache[cache_key] = instance
            return instance
        except Exception as e:
            logger.error("Failed to load room handler %r: %s", handler_ref, e)
            logger.debug("room handler load exception", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Turn lifecycle helpers
    # ------------------------------------------------------------------

    def _register_reply_route(self, *, envelope: InboundEnvelope) -> None:
        try:
            self._reply_router.set_route(envelope.request_id, envelope.reply_to)
        except Exception as e:
            logger.error(
                "Failed setting %s reply route for request_id=%s: %s",
                envelope.surface,
                envelope.request_id,
                e,
            )
            logger.debug("reply route set exception details", exc_info=True)

    def _persist_inbound_turn(
            self,
            *,
            adapter: InboundSurfaceAdapter,
            persist_unified_log: bool,
    ) -> None:
        if not adapter.persist_to_history:
            return
        inbound_msg = adapter.append_inbound()
        if persist_unified_log and inbound_msg is not None:
            self._persist_message_to_unified_log(message=inbound_msg, source=adapter.inbound_source)

    def _prepare_turn_context(
            self,
            *,
            envelope: InboundEnvelope,
            room_ctx: Dict[str, Any],
            room_contact_name: str,
            adapter: InboundSurfaceAdapter,
    ) -> tuple[Dict[str, Any], str]:
        effective_room_ctx = dict(room_ctx) if isinstance(room_ctx, dict) else {}
        incoming_meta = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        room_mode = self.ingress_service._normalize_room_mode(incoming_meta.get("room_mode"))
        room_policy = effective_room_ctx.get("room_policy") if isinstance(effective_room_ctx.get("room_policy"), dict) else {}
        mode_overrides = room_policy.get("mode_manager_overrides") if isinstance(room_policy.get("mode_manager_overrides"), dict) else {}
        override_manager = str(mode_overrides.get(room_mode) or "").strip() if isinstance(mode_overrides, dict) else ""
        if override_manager:
            effective_room_ctx["room_manager_name"] = override_manager

        allowed_resource_ids = room_ctx.get("room_allowed_global_resources") if isinstance(room_ctx, dict) else None
        selected_resource_ids = self.resource_injection_service.select_resource_ids_for_injection(
            allowed_resource_ids=allowed_resource_ids if isinstance(allowed_resource_ids, list) else [],
            inbound_text=str(envelope.content or ""),
        )
        request_data = self.ingress_service.build_request_data(
            room_ctx=effective_room_ctx,
            envelope=envelope,
            room_contact_name=room_contact_name,
            allowed_resource_context="",
        )
        # Resolve the sticky actas principal for this room (set by /actas
        # slash command) so the scope builder can stamp ScopeContext.acting_as.
        actas_principal = "user"
        actas_service = getattr(self.ingress_service, "_actas_sessions", None)
        if actas_service is not None:
            try:
                actas_principal = actas_service.get_principal(
                    room_id=envelope.room_id or "",
                    surface=envelope.surface or "",
                    context_id=envelope.context_id or "main",
                )
            except Exception:
                logger.debug("actas_session_service.get_principal failed", exc_info=True)
                actas_principal = "user"
        request_data["actas_principal"] = actas_principal
        request_data["scope_contract"] = build_scope_contract_for_room_request(
            room_ctx=effective_room_ctx,
            envelope=envelope,
            request_data=request_data,
        )
        scope_contract = request_data.get("scope_contract")
        if not isinstance(scope_contract, dict):
            raise ValueError("Room scope_contract must be a dict.")
        request_data["room_allowed_resource_context"] = self.resource_injection_service.build_allowed_resource_context(
            selected_resource_ids,
            scope_context=scope_contract,
        )
        current_room_mode = str(incoming_meta.get("room_mode") or "normal").strip()
        normalized_room_mode = self.ingress_service._normalize_room_mode(current_room_mode)
        mode_session_key_map = {
            "planning_mode": "plan_session_id",
            "task_creation_mode": "task_creation_session_id",
            "doc_creation_mode": "doc_creation_session_id",
            "game_mode": "geo_session_id",
        }
        mode_session_key = mode_session_key_map.get(normalized_room_mode, "")
        current_mode_session_id = str(incoming_meta.get(mode_session_key) or "").strip() if mode_session_key else ""
        history_policy = room_policy.get("history") if isinstance(room_policy, dict) else None
        request_data["seeded_chat_messages"] = self._build_room_session_seeded_messages(
            room_id=envelope.room_id,
            limit=None,
            room_surface=envelope.surface,
            room_context_id=envelope.context_id,
            shared_chat_room_ids=effective_room_ctx.get("room_shared_chat_room_ids", []) if isinstance(effective_room_ctx, dict) else [],
            current_room_mode=current_room_mode,
            current_mode_session_id=current_mode_session_id,
            history_policy=history_policy,
        )
        logger.info(
            "[HISTORY_DIAG:B] seeded_chat_messages built: len=%d room_id=%s mode=%s session_id=%s",
            len(request_data["seeded_chat_messages"]),
            envelope.room_id,
            current_room_mode,
            current_mode_session_id,
        )
        manager_name = resolve_room_manager_name(effective_room_ctx)
        return request_data, manager_name

    def _run_room_controller(
            self,
            *,
            envelope: InboundEnvelope,
            request_data: Dict[str, Any],
            manager_name: str,
            send_reply: bool,
    ):
        req = self.ingress_service.build_manager_request(envelope=envelope, request_data=request_data)
        manager_result = self.ingress_service.invoke_room_manager(manager_name=manager_name, manager_request=req)
        return self.post_room_service.build_outbound_intent(
            request_id=envelope.request_id,
            room_id=envelope.room_id,
            room_surface=envelope.surface,
            room_context_id=envelope.context_id,
            reply_to=envelope.reply_to,
            manager_result=manager_result,
            send_reply=send_reply,
        )

    def _deliver_outbound(
            self,
            *,
            envelope: InboundEnvelope,
            adapter: InboundSurfaceAdapter,
            reply_text: str,
            should_send: bool,
    ) -> Any:
        if not should_send:
            return None
        try:
            return adapter.send_outbound(reply_text)
        except Exception as e:
            logger.error("Failed sending %s reply for room_id=%s: %s", envelope.surface, envelope.room_id, e)
            logger.debug("send reply exception details", exc_info=True)
            return None

    def _persist_outbound_turn(
            self,
            *,
            adapter: InboundSurfaceAdapter,
            persist_unified_log: bool,
            reply_text: str,
            outbound_result: Any,
    ) -> None:
        if not adapter.persist_to_history or not str(reply_text or "").strip():
            return
        outbound_msg = adapter.append_outbound(reply_text, outbound_result)
        if persist_unified_log and outbound_msg is not None:
            self._persist_message_to_unified_log(message=outbound_msg, source=adapter.outbound_source)

    @staticmethod
    def _build_turn_response(
            *,
            envelope: InboundEnvelope,
            adapter: InboundSurfaceAdapter,
            message_persistence_mode: str,
            persist_unified_log: bool,
            persist_reason: str,
            send_reply: bool,
            outbound_intent,
            outbound_result: Any,
    ) -> Dict[str, Any]:
        response: Dict[str, Any] = {
            "request_id": envelope.request_id,
            "room_id": envelope.room_id,
            "reply_text": outbound_intent.reply_text,
            "reply_payload": outbound_intent.payload,
            "outbound_intent": outbound_intent.to_dict(),
            "message_persistence_mode": message_persistence_mode,
            "unified_log_persistence_enabled": persist_unified_log,
            "unified_log_persistence_reason": persist_reason,
        }
        if isinstance(adapter.outbound_result_key, str) and adapter.outbound_result_key.strip():
            response[adapter.outbound_result_key.strip()] = outbound_result
        if adapter.include_dry_run and not send_reply:
            response["dry_run"] = True
        return response

    def _maybe_trigger_room_summary(self, *, room_id: str, room_ctx: Dict[str, Any] | None) -> None:
        if not isinstance(room_id, str) or not room_id.strip():
            return
        try:
            from app.assistant.room_session_manager.services.room_policy_service import resolve_room_chat_compaction
            enabled, summary_agent = resolve_room_chat_compaction(room_ctx)
            if not enabled:
                # Opt-in: a room only summarizes if it declared chat_compaction in
                # its ROOM.md policy. Unregistered rooms are skipped here.
                return
            from app.assistant.room_session_manager.services.room_summary_service import maybe_trigger_room_summary
            maybe_trigger_room_summary(room_id=room_id, summary_agent=summary_agent)
        except Exception as e:
            logger.error("Failed triggering room summary for room_id=%s: %s", room_id, e)
            logger.debug("room summary trigger exception details", exc_info=True)

    # ------------------------------------------------------------------
    # Main inbound handler (generic across all surfaces)
    # ------------------------------------------------------------------

    def _handle_inbound_generic(
            self,
            *,
            envelope: InboundEnvelope,
            room_ctx: Dict[str, Any],
            room_contact_name: str,
            send_reply: bool,
            message_persistence_mode: str,
            persist_unified_log: bool,
            persist_reason: str,
            adapter: InboundSurfaceAdapter,
    ) -> Dict[str, Any]:
        if str(envelope.room_id or "").strip() == "master_room":
            try:
                block_dayflow_orchestrator_for_master_chat(request_id=envelope.request_id)
                logger.debug(
                    "Updated dayflow orchestrator block timer from master_room ingress. request_id=%s",
                    envelope.request_id,
                )
            except Exception as e:
                logger.error("Failed updating dayflow orchestrator block timer from master_room ingress: %s", e)
                logger.debug("dayflow orchestrator block timer update exception details", exc_info=True)

            try:
                from app.assistant.utils.subsystem_flags import is_subsystem_enabled
                if is_subsystem_enabled("context_engine"):
                    from app.assistant.context_engine.pipeline import maybe_trigger_pipeline
                    from app.assistant.kg_core.user_identity import get_primary_user_name

                    primary_user = get_primary_user_name()
                    owner_id = str(envelope.room_id or "").strip()
                    user_message = str(envelope.content or "").strip()

                    if user_message:
                        triggered = maybe_trigger_pipeline(
                            user_message=user_message,
                            primary_user=primary_user,
                            owner_id=owner_id,
                        )
                        logger.debug(
                            "context_engine trigger: triggered=%s owner_id=%r request_id=%s",
                            triggered,
                            owner_id,
                            envelope.request_id,
                        )
            except Exception as e:
                logger.error("context_engine trigger failed (non-fatal): %s", e)
                logger.debug("context_engine trigger exception details", exc_info=True)

        self._register_reply_route(envelope=envelope)
        self._persist_inbound_turn(adapter=adapter, persist_unified_log=persist_unified_log)

        # Room handler hook: load persistent state (spec/doc) onto the global
        # blackboard BEFORE _prepare_turn_context builds request_data from it.
        room_handler = self._get_room_handler(room_ctx)
        if room_handler is not None:
            room_handler.prepare_turn(
                blackboard=self._blackboard,
                surface=str(envelope.surface or "ui"),
                context_id=str(envelope.context_id or "main"),
                room_id=str(envelope.room_id or ""),
            )

        request_data, manager_name = self._prepare_turn_context(
            envelope=envelope,
            room_ctx=room_ctx,
            room_contact_name=room_contact_name,
            adapter=adapter,
        )

        outbound_intent = self._run_room_controller(
            envelope=envelope,
            request_data=request_data,
            manager_name=manager_name,
            send_reply=send_reply,
        )
        outbound_result = self._deliver_outbound(
            envelope=envelope,
            adapter=adapter,
            reply_text=outbound_intent.reply_text,
            should_send=bool(outbound_intent.send),
        )
        self._persist_outbound_turn(
            adapter=adapter,
            persist_unified_log=persist_unified_log,
            reply_text=outbound_intent.reply_text,
            outbound_result=outbound_result,
        )
        maybe_write_mode_exit_summary(
            blackboard=self._blackboard,
            envelope=envelope,
            payload=outbound_intent.payload,
            reply_text=outbound_intent.reply_text,
        )
        # Only trigger room summary for normal mode — task creation, doc creation,
        # planning mode messages should not be compressed into the room history.
        envelope_mode = ""
        if isinstance(envelope.metadata, dict):
            envelope_mode = str(envelope.metadata.get("room_mode") or "").strip().lower()
        if envelope_mode in ("", "normal"):
            self._maybe_trigger_room_summary(room_id=envelope.room_id, room_ctx=room_ctx)
        ensure_request_room_metadata(
            blackboard=self._blackboard,
            request_id=envelope.request_id,
            room_id=envelope.room_id,
            room_surface=envelope.surface,
            room_context_id=envelope.context_id,
            manager_name=manager_name,
        )
        return self._build_turn_response(
            envelope=envelope,
            adapter=adapter,
            message_persistence_mode=message_persistence_mode,
            persist_unified_log=persist_unified_log,
            persist_reason=persist_reason,
            send_reply=send_reply,
            outbound_intent=outbound_intent,
            outbound_result=outbound_result,
        )

    # ------------------------------------------------------------------
    # Surface adapter builders
    # ------------------------------------------------------------------

    def _build_telegram_adapter(
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
    ) -> InboundSurfaceAdapter:
        return InboundSurfaceAdapter(
            inbound_source="room_telegram",
            outbound_source="room_telegram",
            append_inbound=lambda: self.message_persistence_service.append_telegram_inbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                chat_id=chat_id,
                message_id=message_id,
                sender_name=sender_name or room_contact_name,
                sender_id=sender_id,
                body=body,
            ),
            append_outbound=lambda reply_text, outbound_message_id: self.message_persistence_service.append_telegram_outbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                chat_id=chat_id,
                reply_text=reply_text,
                outbound_message_id=outbound_message_id,
            ),
            send_outbound=lambda reply_text: self.telegram_transport.send_reply(chat_id=chat_id, body=reply_text),
            outbound_result_key="outbound_message_id",
        )

    def _build_ui_adapter(
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
    ) -> InboundSurfaceAdapter:
        return InboundSurfaceAdapter(
            inbound_source="room_ui",
            outbound_source="room_ui",
            append_inbound=lambda: self.message_persistence_service.append_ui_inbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                room_context_id=room_context_id,
                sender_name=sender_name,
                content=content,
                socket_id=socket_id,
                route_meta=route_meta,
                inbound_ts_local=inbound_ts_local,
            ),
            append_outbound=lambda reply_text, _outbound_status: self.message_persistence_service.append_ui_outbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                room_context_id=room_context_id,
                reply_text=reply_text,
                socket_id=socket_id,
                route_meta=route_meta,
            ),
            send_outbound=lambda reply_text: self.ui_transport.send_reply(  # type: ignore[union-attr]
                room_id=room_id,
                body=reply_text,
                request_id=request_id,
                metadata=route_meta,
            ),
        )

    def _build_sms_adapter(
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
    ) -> InboundSurfaceAdapter:
        return InboundSurfaceAdapter(
            inbound_source="room_sms",
            outbound_source="room_sms",
            append_inbound=lambda: self.message_persistence_service.append_sms_inbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                from_number=from_number,
                to_number=to_number,
                message_sid=message_sid,
                body=body,
                room_inbound_timestamp_local=room_inbound_timestamp_local,
            ),
            append_outbound=lambda reply_text, outbound_sid: self.message_persistence_service.append_sms_outbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                from_number=from_number,
                to_number=to_number,
                reply_text=reply_text,
                twilio_sid=outbound_sid,
            ),
            send_outbound=lambda reply_text: self.sms_transport.send_reply(
                to_number=from_number,
                from_number=to_number,
                body=reply_text,
            ),
            outbound_result_key="outbound_message_sid",
        )

    def _build_slack_adapter(
            self,
            *,
            request_id: str,
            room_id: str,
            room_contact_name: str,
            room_context_id: str,
            sender_name: str,
            sender_id: str,
            inbound_content: str,
            channel_id: str,
            message_ts: str,
            thread_ts: str,
            allow_images: bool,
            simulated: bool,
            inbound_ts_local: str,
            persist_to_history: bool,
    ) -> InboundSurfaceAdapter:
        return InboundSurfaceAdapter(
            inbound_source="room_slack",
            outbound_source="room_slack",
            append_inbound=lambda: self.message_persistence_service.append_slack_inbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                room_context_id=room_context_id,
                sender_name=sender_name,
                sender_id=sender_id,
                content=inbound_content,
                channel_id=channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                allow_images=allow_images,
                simulated=simulated,
                inbound_ts_local=inbound_ts_local,
            ),
            append_outbound=lambda reply_text, _outbound_status: self.message_persistence_service.append_slack_outbound(
                request_id=request_id,
                room_id=room_id,
                room_contact_name=room_contact_name,
                room_context_id=room_context_id,
                reply_text=reply_text,
                channel_id=channel_id,
                thread_ts=thread_ts,
                simulated=simulated,
            ),
            send_outbound=lambda reply_text: self.slack_transport.send_reply(
                channel_id=channel_id,
                body=reply_text,
                thread_ts=thread_ts,
            ),
            outbound_result_key="outbound_status",
            include_dry_run=True,
            persist_to_history=persist_to_history,
        )

    # ------------------------------------------------------------------
    # Public surface handlers
    # ------------------------------------------------------------------

    def handle_telegram_inbound(
            self,
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
        return self.surface_handler_factory.get("telegram").handle(
            self,
            chat_id=chat_id,
            body=body,
            room_id=room_id,
            message_id=message_id,
            sender_name=sender_name,
            sender_id=sender_id,
            send_reply=send_reply,
            message_persistence_mode=message_persistence_mode,
        )

    def handle_ui_inbound(
            self,
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
        return self.surface_handler_factory.get("ui").handle(
            self,
            socket_id=socket_id,
            body=body,
            room_id=room_id,
            request_id=request_id,
            sender_name=sender_name,
            send_reply=send_reply,
            message_persistence_mode=message_persistence_mode,
            metadata=metadata,
        )

    def handle_sms_inbound(
            self,
            *,
            from_number: str,
            to_number: str,
            body: str,
            message_sid: str,
            room_id: str,
            send_reply: bool = False,
            message_persistence_mode: str = "global_blackboard_and_unified_log",
    ) -> Dict[str, Any]:
        return self.surface_handler_factory.get("sms").handle(
            self,
            from_number=from_number,
            to_number=to_number,
            body=body,
            message_sid=message_sid,
            room_id=room_id,
            send_reply=send_reply,
            message_persistence_mode=message_persistence_mode,
        )

    def handle_slack_inbound(
            self,
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
        return self.surface_handler_factory.get("slack").handle(
            self,
            channel_id=channel_id,
            body=body,
            room_id=room_id,
            message_ts=message_ts,
            sender_name=sender_name,
            sender_id=sender_id,
            thread_ts=thread_ts,
            image_paths=image_paths,
            send_reply=send_reply,
            persist_to_history=persist_to_history,
            message_persistence_mode=message_persistence_mode,
            simulated=simulated,
        )
