from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.services.plan_session_service import PlanSessionService
from app.assistant.room_session_manager.services.task_creation_session_service import TaskCreationSessionService
from app.assistant.room_session_manager.services.doc_creation_session_service import DocCreationSessionService
from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
from app.assistant.room_session_manager.services.room_slash_command_router import RoomSlashCommandRouter
from app.assistant.room_session_manager.services.room_mention_router import RoomMentionRouter
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RoomIngressService:
    def __init__(
        self,
        *,
        plan_session_service: PlanSessionService | None = None,
        task_creation_session_service: TaskCreationSessionService | None = None,
        doc_creation_session_service: DocCreationSessionService | None = None,
        geo_session_service: GeoguessrSessionService | None = None,
        manager_registry: Any = None,
        multi_agent_manager_factory: Any = None,
        manager_invoker: Any = None,
    ) -> None:
        self._plan_sessions = plan_session_service
        self._task_sessions = task_creation_session_service
        self._doc_sessions = doc_creation_session_service
        self._geo_sessions = geo_session_service
        self._manager_registry = manager_registry
        self._multi_agent_manager_factory = multi_agent_manager_factory
        self._manager_invoker = manager_invoker
        self._slash_router = RoomSlashCommandRouter(
            plan_sessions=plan_session_service,
            task_sessions=task_creation_session_service,
            doc_sessions=doc_creation_session_service,
            geo_sessions=geo_session_service,
        )
        # Built lazily on first use so DI is available (mailbox + active
        # worker resolvers are registered after RoomIngressService).
        self._mention_router: RoomMentionRouter | None = None

    def _get_mention_router(self) -> RoomMentionRouter | None:
        if self._mention_router is not None:
            return self._mention_router
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.manager_runtime.active_workers import (
                find_active_invocation_by_display_name,
                list_active_display_names,
                list_active_workers,
            )
            mailbox = getattr(DI, "mailbox", None)
            if mailbox is None:
                return None
            self._mention_router = RoomMentionRouter(
                active_workers_provider=list_active_workers,
                worker_resolver=find_active_invocation_by_display_name,
                display_names_lister=list_active_display_names,
                mailbox=mailbox,
            )
            return self._mention_router
        except Exception:
            logger.debug("RoomMentionRouter unavailable", exc_info=True)
            return None

    @staticmethod
    def extract_slash_command(content: str) -> tuple[str, str] | None:
        """
        Parse slash commands only when the FIRST token is a slash command.
        Examples:
        - "/plan research X" -> ("plan", "research X")
        - "  /done" -> ("done", "")
        - "hello /plan" -> None
        """
        raw = str(content or "")
        if not raw.strip():
            return None
        match = re.match(r"^\s*/([A-Za-z0-9_:-]+)(?:\s+(.*))?\s*$", raw, flags=re.DOTALL)
        if not match:
            return None
        cmd = str(match.group(1) or "").strip().lower()
        payload = str(match.group(2) or "").strip()
        if not cmd:
            return None
        return cmd, payload

    @staticmethod
    def _normalize_room_mode(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "normal"
        if raw in {"plan", "planner", "planning", "planning_mode"}:
            return "planning_mode"
        if raw in {"task", "task_create", "task_creation", "task_creation_mode"}:
            return "task_creation_mode"
        if raw in {"doc", "doc_create", "doc_creation", "doc_creation_mode"}:
            return "doc_creation_mode"
        if raw in {"game", "game_mode", "geoguessr"}:
            return "game_mode"
        return raw

    def _resolve_manager_flow_config(self, *, room_ctx: Dict[str, Any]) -> Dict[str, Any] | None:
        direct_flow_cfg = room_ctx.get("flow_config")
        if isinstance(direct_flow_cfg, dict):
            return direct_flow_cfg
        manager_name = room_ctx.get("room_manager_name")
        if not isinstance(manager_name, str) or not manager_name.strip():
            return None
        if self._manager_registry is None:
            return None
        manager_cfg = self._manager_registry.get(manager_name.strip())
        if not isinstance(manager_cfg, dict):
            return None
        flow_cfg = manager_cfg.get("flow_config")
        return flow_cfg if isinstance(flow_cfg, dict) else None

    def _resolve_mode_source_agent(self, *, room_ctx: Dict[str, Any], room_mode: str) -> str | None:
        flow_cfg = self._resolve_manager_flow_config(room_ctx=room_ctx)
        if not isinstance(flow_cfg, dict):
            return None
        flow = flow_cfg.get("flow")
        if not isinstance(flow, dict):
            return None
        mode_key = self._normalize_room_mode(room_mode)
        mode_cfg = flow.get(mode_key)
        # If mode not found, check room policy for a default_room_mode.
        if not isinstance(mode_cfg, dict):
            policy = room_ctx.get("room_policy") if isinstance(room_ctx, dict) else None
            default_mode = str((policy or {}).get("default_room_mode") or "").strip()
            if default_mode:
                mode_key = self._normalize_room_mode(default_mode)
                mode_cfg = flow.get(mode_key)
        if not isinstance(mode_cfg, dict):
            raise ValueError(f"room flow mode '{mode_key}' is not configured under flow_config.flow")
        source_agent = mode_cfg.get("source_agent")
        if not isinstance(source_agent, str) or not source_agent.strip():
            raise ValueError(f"flow_config.flow.{mode_key}.source_agent must be a non-empty string")
        return source_agent.strip()

    def apply_room_mode_context(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        body: str,
        metadata: Dict[str, Any] | None,
        sender_identity: str,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any] | None]:
        """
        Apply slash-command and active session routing logic.
        Returns: (normalized_body, normalized_metadata, early_result)
        """
        meta = dict(metadata) if isinstance(metadata, dict) else {}

        # @mention takes precedence — runs before slash so e.g. "@em /plan ..."
        # would still address Em (unlikely but consistent: leading token wins).
        mention_router = self._get_mention_router()
        if mention_router is not None:
            mention_result = mention_router.route(
                body=body, room_id=room_id, meta=meta,
            )
            if mention_result is not None:
                # @mention always short-circuits — the manager pipeline never
                # runs for the addressed message.
                return "", mention_result.meta, mention_result.early_result

        cmd = self.extract_slash_command(body)
        if cmd is not None:
            cmd_name, cmd_payload = cmd
            result = self._slash_router.route(
                cmd_name=cmd_name,
                cmd_payload=cmd_payload,
                room_id=room_id,
                surface=surface,
                context_id=context_id,
                sender_identity=sender_identity,
                meta=meta,
            )
            if result is not None:
                if not result.continue_pipeline:
                    return "", result.meta, result.early_result
                return result.normalized_body, result.meta, None
            # Unrecognized slash command — do not pass to agent pipeline.
            logger.info("[RoomIngressService] Unknown slash command: /%s", cmd_name)
            return "", meta, {
                "reply_text": f"Unknown command: /{cmd_name}. Type /help for available commands.",
                "reply_payload": {"chat": f"Unknown command: /{cmd_name}. Type /help for available commands."},
                "outbound_intent": {
                    "send": True,
                    "reply_text": f"Unknown command: /{cmd_name}. Type /help for available commands.",
                    "reply_payload": {"chat": f"Unknown command: /{cmd_name}. Type /help for available commands."},
                },
            }
        active = self._plan_sessions.get_active_room_binding(
            room_id=room_id,
            surface=surface,
            context_id=context_id,
        ) if self._plan_sessions is not None else None
        if isinstance(active, dict):
            meta["room_mode"] = "planning_mode"
            plan_session_id = str(active.get("plan_session_id") or "").strip()
            if plan_session_id:
                meta["plan_session_id"] = plan_session_id
            return body, meta, None

        active_task = self._task_sessions.get_active_room_binding(
            room_id=room_id,
            surface=surface,
            context_id=context_id,
        ) if self._task_sessions is not None else None
        if isinstance(active_task, dict):
            meta["room_mode"] = "task_creation_mode"
            task_session_id = str(active_task.get("session_id") or "").strip()
            if task_session_id:
                meta["task_creation_session_id"] = task_session_id
            return body, meta, None

        if self._doc_sessions is not None and room_id == "doc_editor":
            active_doc = self._doc_sessions.get_active_room_binding(
                room_id=room_id,
                surface=surface,
                context_id=context_id,
            )
            if isinstance(active_doc, dict):
                meta["room_mode"] = "doc_creation_mode"
                doc_session_id = str(active_doc.get("session_id") or "").strip()
                if doc_session_id:
                    meta["doc_creation_session_id"] = doc_session_id
                doc_type = str(active_doc.get("doc_type") or "md").strip()
                meta["doc_type"] = doc_type
                active_doc_name = str(active_doc.get("doc_name") or "").strip()
                if active_doc_name:
                    meta["doc_name"] = active_doc_name
                return body, meta, None

        if self._geo_sessions is not None:
            active_geo = self._geo_sessions.get_active_room_binding(
                room_id=room_id,
                surface=surface,
                context_id=context_id,
            )
            if isinstance(active_geo, dict):
                meta["room_mode"] = "game_mode"
                geo_session_id = str(active_geo.get("session_id") or "").strip()
                if geo_session_id:
                    meta["geo_session_id"] = geo_session_id
                return body, meta, None

        return body, meta, None
    @staticmethod
    def _build_information_line(envelope: InboundEnvelope) -> str:
        surface = envelope.surface
        speaker = envelope.speaker_name or "Unknown"
        ts_local = envelope.timestamp_local or "unknown_time"
        if surface == "ui":
            socket_id = str((envelope.extras or {}).get("socket_id") or envelope.transport_to or "").strip()
            return (
                f"Inbound UI chat message from {speaker} "
                f"(socket_id={socket_id or 'unknown'}). "
                f"Received at local time {ts_local}. "
                f"Latest inbound line: {envelope.inbound_line}"
            )
        if surface == "sms":
            from_number = str(envelope.transport_from or "").strip()
            to_number = str(envelope.transport_to or "").strip()
            return (
                f"Inbound SMS from {speaker} ({from_number}) to {to_number}. "
                f"Received at local time {ts_local}. "
                f"Latest inbound line: {envelope.inbound_line}"
            )
        if surface == "telegram":
            sender_id = str((envelope.extras or {}).get("sender_id") or envelope.transport_from or "").strip()
            chat_id = str((envelope.extras or {}).get("chat_id") or envelope.transport_to or "").strip()
            return (
                f"Inbound Telegram message from {speaker} ({sender_id or 'unknown_user'}) "
                f"in chat {chat_id or 'unknown_chat'}. "
                f"Received at local time {ts_local}. "
                f"Latest inbound line: {envelope.inbound_line}"
            )
        channel_id = str((envelope.extras or {}).get("channel_id") or envelope.transport_to or "").strip()
        sender_id = str((envelope.extras or {}).get("sender_id") or envelope.speaker_external_id or "unknown_user").strip()
        return (
            f"Inbound Slack message from {speaker} ({sender_id}) "
            f"in channel {channel_id}. "
            f"Received at local time {ts_local}. "
            f"Latest inbound line: {envelope.inbound_line}"
        )

    def build_request_data(
        self,
        *,
        room_ctx: Dict[str, Any],
        envelope: InboundEnvelope,
        room_contact_name: str,
        allowed_resource_context: str,
    ) -> Dict[str, Any]:
        if not isinstance(room_ctx, dict):
            room_ctx = {}
        request_data: Dict[str, Any] = {
            **room_ctx,
            "room_id": envelope.room_id,
            "room_contact_name": room_contact_name,
            # current_speaker_name is who said THIS inbound message, which
            # may differ from room_contact_name in multi-participant rooms
            # (e.g., a Slack channel with Jukka + Justin).
            "current_speaker_name": str(envelope.speaker_name or "").strip(),
            "room_inbound_timestamp_local": envelope.timestamp_local,
            "room_inbound_line": envelope.inbound_line,
            "room_surface": envelope.surface,
            "room_context_id": envelope.context_id,
            "room_visibility": "room_shared",
            "room_allowed_resource_context": allowed_resource_context,
        }
        extras = envelope.extras if isinstance(envelope.extras, dict) else {}
        for key, value in extras.items():
            request_data[key] = value
        metadata = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        # Lift reply_to from envelope metadata onto request_data so the
        # scope builder can put it on ScopeContext. From there it
        # propagates through chained sub-managers via existing scope flow,
        # giving every layer a single canonical "where do I send back?"
        # field. Surfaces (UI/slack/sms/telegram inbounds) all set
        # ``metadata["reply_to"]`` already.
        env_reply_to = metadata.get("reply_to")
        if isinstance(env_reply_to, dict) and env_reply_to:
            request_data["reply_to"] = dict(env_reply_to)
        plan_session_id = metadata.get("plan_session_id")
        ticket_id = metadata.get("ticket_id")
        task_creation_session_id = metadata.get("task_creation_session_id")
        doc_creation_session_id = metadata.get("doc_creation_session_id")
        doc_type = metadata.get("doc_type")
        room_mode = metadata.get("room_mode")
        if isinstance(room_mode, str) and room_mode.strip():
            request_data["room_mode"] = self._normalize_room_mode(room_mode)
        if isinstance(plan_session_id, str) and plan_session_id.strip():
            request_data["room_mode"] = "planning_mode"
            request_data["plan_session_id"] = plan_session_id.strip()
            if isinstance(ticket_id, str) and ticket_id.strip():
                request_data["plan_ticket_id"] = ticket_id.strip()
        elif isinstance(task_creation_session_id, str) and task_creation_session_id.strip():
            request_data["room_mode"] = "task_creation_mode"
            request_data["task_creation_session_id"] = task_creation_session_id.strip()
            # Restore persisted task draft so task_spec_writer can do incremental edits
            # across turns instead of starting from an empty spec each request.
            if self._task_sessions is not None:
                try:
                    task_sessions = self._task_sessions._load_sessions()
                    task_payload = task_sessions.get(task_creation_session_id.strip())
                    if isinstance(task_payload, dict):
                        stored_draft = str(task_payload.get("draft_spec") or "").strip()
                        if stored_draft:
                            request_data["spec"] = stored_draft
                        created_at = task_payload.get("created_at_utc")
                        if isinstance(created_at, str) and created_at.strip():
                            request_data["task_session_start_utc"] = created_at.strip()
                except Exception as e:
                    logger.error(
                        "Failed to restore task draft for session %s: %s",
                        task_creation_session_id,
                        e,
                    )
                    logger.debug("task draft restore exception", exc_info=True)
                    raise
        elif isinstance(doc_creation_session_id, str) and doc_creation_session_id.strip():
            request_data["room_mode"] = "doc_creation_mode"
            session_id_norm = doc_creation_session_id.strip()
            request_data["doc_creation_session_id"] = session_id_norm
            if isinstance(doc_type, str) and doc_type.strip():
                request_data["doc_type"] = doc_type.strip()
            doc_name = metadata.get("doc_name")
            if isinstance(doc_name, str) and doc_name.strip():
                request_data["doc_name"] = doc_name.strip()
            # Restore the persisted doc content from unified_log doc_store so the flow
            # blackboard has it from turn start. Without this, doc_markdown_last_emitted
            # is only written at end of turn N, meaning the doc_writer on turn N+1 would
            # see an empty doc.
            try:
                from app.assistant.lib.doc_utils.doc_store import load_doc_draft
                draft = load_doc_draft(session_id_norm)
                if draft is not None:
                    stored_markdown = str(draft.get("doc_markdown") or "").strip()
                    if stored_markdown:
                        request_data["doc_markdown_last_emitted"] = stored_markdown
                    stored_gdoc_id = str(draft.get("google_doc_id") or "").strip()
                    if stored_gdoc_id:
                        request_data["doc_id"] = stored_gdoc_id
                    if "doc_name" not in request_data:
                        stored_name = str(draft.get("doc_name") or "").strip()
                        if stored_name:
                            request_data["doc_name"] = stored_name
                    created_at = draft.get("created_at")
                    if isinstance(created_at, str) and created_at.strip():
                        request_data["doc_session_start_utc"] = created_at.strip()
            except Exception as e:
                logger.warning("Failed to restore doc_markdown for session %s: %s", doc_creation_session_id, e)
                logger.debug("doc_markdown restore exception", exc_info=True)
        if "room_mode" not in request_data:
            request_data["room_mode"] = "normal"

        # Propagate TTS/interaction-mode flags from inbound metadata onto request_data so
        # they land on the blackboard and are available to control nodes (e.g. geoguessr_router_node).
        tts_flag = metadata.get("tts_requested")
        if tts_flag is not None:
            # Parse explicitly — bool("false") is True in Python.
            if isinstance(tts_flag, str):
                request_data["tts_requested"] = tts_flag.strip().lower() in ("true", "1", "yes")
            else:
                request_data["tts_requested"] = bool(tts_flag)
        interaction_mode = metadata.get("interaction_mode")
        if isinstance(interaction_mode, str) and interaction_mode.strip():
            request_data["interaction_mode"] = interaction_mode.strip()

        source_agent = self._resolve_mode_source_agent(
            room_ctx=room_ctx,
            room_mode=request_data.get("room_mode"),
        )
        if request_data.get("room_mode") != "normal" and not source_agent:
            raise RuntimeError(
                f"Room mode '{request_data.get('room_mode')}' has no source agent mapping "
                f"for room_id={envelope.room_id} manager={room_ctx.get('room_manager_name')!r}."
            )
        if isinstance(source_agent, str) and source_agent:
            # Room-mode source selection is an ingress concern.
            request_data["next_agent"] = source_agent

        room_permissions = request_data.get("room_permissions")
        if isinstance(room_permissions, dict):
            allowed = room_permissions.get("allowed_tools")
            blocked = room_permissions.get("blocked_tools")
            if isinstance(allowed, list):
                request_data["task_allowed_tools"] = [str(t).strip() for t in allowed if isinstance(t, str) and str(t).strip()]
            if isinstance(blocked, list):
                request_data["task_except_tools"] = [str(t).strip() for t in blocked if isinstance(t, str) and str(t).strip()]

        return request_data

    def build_manager_request(self, *, envelope: InboundEnvelope, request_data: Dict[str, Any]) -> Message:
        metadata = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        scope_contract = request_data.get("scope_contract") if isinstance(request_data.get("scope_contract"), dict) else None
        return Message(
            data_type="user_message",
            sender=envelope.speaker_name,
            receiver=None,
            timestamp=datetime.now(timezone.utc),
            id=str(uuid.uuid4()),
            request_id=envelope.request_id,
            role="user",
            event_topic=None,
            agent_input=envelope.content,
            task=envelope.content,
            information=self._build_information_line(envelope),
            metadata=metadata,
            room_id=envelope.room_id,
            room_surface=envelope.surface,
            room_context_id=envelope.context_id,
            room_visibility="room_shared",
            room_policy_id=f"room_policy::{envelope.room_id}::v1",
            room_message_direction="inbound",
            room_initiated_by="user",
            room_delivery_mode="auto_send",
            room_speaker_id=envelope.speaker_id,
            room_speaker_name=envelope.speaker_name,
            room_speaker_role="participant",
            room_speaker_external_id=envelope.speaker_external_id,
            room_actor_id=envelope.speaker_id,
            transport_message_id=envelope.transport_message_id or None,
            transport_from=envelope.transport_from or None,
            transport_to=envelope.transport_to or None,
            data=request_data,
            scope_context=scope_contract,
        )

    def invoke_room_manager(self, *, manager_name: str, manager_request: Message) -> Any:
        if self._multi_agent_manager_factory is None or self._manager_invoker is None:
            raise RuntimeError(
                "RoomIngressService.invoke_room_manager requires multi_agent_manager_factory "
                "and manager_invoker — pass them via __init__."
            )
        manager = self._multi_agent_manager_factory.create_manager(manager_name)
        return self._manager_invoker.invoke(manager, manager_request)
