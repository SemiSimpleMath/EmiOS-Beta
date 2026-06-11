from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.message_visibility_policy import (
    normalize_room_scope_filters,
    should_include_chat_message,
)
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import day_reset_cutoff_utc, to_utc, utc_to_local
from app.models.base import get_session

logger = get_logger(__name__)


class RoomHistoryBuilder:
    _UNIFIED_CHAT_SOURCES = (
        "chat",
        "room_ui",
        "room_sms",
        "room_telegram",
        "room_slack",
        "slack",
    )

    _SUPPRESSED_BY_ROOM_KEY = "context_suppressed_by_room"
    _PINNED_BY_ROOM_KEY = "context_pinned_by_room"
    _PROCESSED_AT_BY_ROOM_KEY = "room_summary_processed_at_by_room"

    @staticmethod
    def _normalize_str(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _message_fingerprint(msg: Any) -> tuple:
        ts = to_utc(getattr(msg, "timestamp", None))
        ts_key = ts.isoformat() if ts is not None else ""
        return (
            str(getattr(msg, "transport_message_id", "") or "").strip(),
            ts_key,
            str(getattr(msg, "room_id", "") or "").strip(),
            str(getattr(msg, "room_surface", "") or "").strip(),
            str(getattr(msg, "room_context_id", "") or "").strip(),
            str(getattr(msg, "sender", "") or "").strip(),
            str(getattr(msg, "content", "") or "").strip(),
        )

    @staticmethod
    def _get_meta(msg: Any) -> dict:
        meta = getattr(msg, "metadata", None)
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def _get_room_meta_dict(cls, msg: Any, key: str) -> dict:
        meta = cls._get_meta(msg)
        value = meta.get(key)
        return value if isinstance(value, dict) else {}

    @classmethod
    def _is_suppressed_for_room(cls, msg: Any, room_id: str) -> bool:
        rid = cls._normalize_str(room_id)
        by_room = cls._get_room_meta_dict(msg, cls._SUPPRESSED_BY_ROOM_KEY)
        return bool(by_room.get(rid, False))

    @classmethod
    def _is_pinned_for_room(cls, msg: Any, room_id: str) -> bool:
        rid = cls._normalize_str(room_id)
        by_room = cls._get_room_meta_dict(msg, cls._PINNED_BY_ROOM_KEY)
        return bool(by_room.get(rid, False))

    @classmethod
    def _is_processed_for_room(cls, msg: Any, room_id: str) -> bool:
        rid = cls._normalize_str(room_id)
        by_room = cls._get_room_meta_dict(msg, cls._PROCESSED_AT_BY_ROOM_KEY)
        value = by_room.get(rid)
        return isinstance(value, str) and bool(value.strip())

    def _timestamp_sort_key(self, msg: Any) -> tuple[int, datetime]:
        ts_utc = to_utc(getattr(msg, "timestamp", None))
        if ts_utc is None:
            return (1, datetime.min.replace(tzinfo=timezone.utc))
        return (0, ts_utc)

    def _load_unified_chat_messages(
            self,
            *,
            room_id: str,
            room_surface: str | None,
            room_context_id: str | None,
            upper_bound_utc: datetime | None,
            shared_room_ids: list[str] | None = None,
            sql_limit: int = 1200,
    ) -> list[Message]:
        session = get_session()
        try:
            sources = list(self._UNIFIED_CHAT_SOURCES)
            if self._is_dayflow_room(room_id):
                sources.append("email")
                sources.append("dayflow_item")

            source_filter = or_(
                UnifiedLog2026.source.in_(sources),
                UnifiedLog2026.source.like("room_summary::%"),
            )

            stmt = (
                select(UnifiedLog2026)
                    .where(source_filter)
                    .order_by(UnifiedLog2026.timestamp.desc())
                    .limit(max(int(sql_limit or 0), 1))
            )

            all_room_ids = [room_id.strip()] if isinstance(room_id, str) and room_id.strip() else []
            for sid in (shared_room_ids or []):
                if isinstance(sid, str) and sid.strip() and sid.strip() not in all_room_ids:
                    all_room_ids.append(sid.strip())

            if len(all_room_ids) == 1:
                stmt = stmt.where(UnifiedLog2026.room_id == all_room_ids[0])
            elif len(all_room_ids) > 1:
                stmt = stmt.where(UnifiedLog2026.room_id.in_(all_room_ids))

            if isinstance(room_surface, str) and room_surface.strip():
                stmt = stmt.where(UnifiedLog2026.room_surface == room_surface.strip())

            if isinstance(room_context_id, str) and room_context_id.strip():
                stmt = stmt.where(UnifiedLog2026.room_context_id == room_context_id.strip())

            if upper_bound_utc is not None:
                stmt = stmt.where(UnifiedLog2026.timestamp < upper_bound_utc)

            rows = session.execute(stmt).scalars().all()
        except Exception as e:
            logger.error("Failed loading unified_log_2026 rows for room history build: %s", e)
            logger.debug("room history unified log read exception details", exc_info=True)
            return []
        finally:
            session.close()

        allowed_room_ids, scoped_surface, scoped_context_id = normalize_room_scope_filters(
            room_id=room_id,
            shared_room_ids=shared_room_ids,
            room_surface=room_surface,
            room_context_id=room_context_id,
        )
        cutoff_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        exclude_tags = {"entity_card_injection", "agent_notification", "slash_command"}

        out: list[Message] = []
        for row in rows:
            try:
                metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
                data_obj = row.data_json if isinstance(row.data_json, dict) else {}

                sub_data_type = metadata.get("sub_data_type")
                if not isinstance(sub_data_type, list):
                    sub_data_type = []

                role = str(row.role or "").strip().lower()
                sender = str(row.speaker_name or "").strip()
                if not sender:
                    if role:
                        sender = role.replace("_", " ").title()
                    else:
                        sender = "Unknown"

                row_source = str(row.source or "").strip().lower()
                is_chat = (
                    row_source in self._UNIFIED_CHAT_SOURCES
                    or row_source.startswith("room_summary::")
                )

                msg = Message(
                    id=str(row.id),
                    timestamp=row.timestamp,
                    role=row.role,
                    sender=sender,
                    content=str(row.message or ""),
                    is_chat=is_chat,
                    sub_data_type=sub_data_type,
                    request_id=row.request_id,
                    room_id=row.room_id,
                    room_surface=row.room_surface,
                    room_context_id=row.room_context_id,
                    room_message_direction=row.direction,
                    room_speaker_id=row.speaker_id,
                    room_speaker_name=row.speaker_name,
                    room_speaker_role=row.speaker_role,
                    room_speaker_external_id=row.speaker_external_id,
                    transport_message_id=row.transport_message_id,
                    transport_from=row.transport_from,
                    transport_to=row.transport_to,
                    metadata={**metadata, "history_source": "unified_log"},
                    data=data_obj,
                )

                if not should_include_chat_message(
                        msg=msg,
                        cutoff_utc=cutoff_epoch,
                        allowed_room_ids=allowed_room_ids,
                        room_surface=scoped_surface,
                        room_context_id=scoped_context_id,
                        include_tags=set(),
                        exclude_tags=exclude_tags,
                        include_command_scopes=set(),
                        include_summarized=True,
                ):
                    continue

                out.append(msg)
            except Exception as e:
                logger.error("Failed normalizing unified log row to Message for room history: %s", e)
                logger.debug("room history unified row normalize exception details", exc_info=True)
                continue

        try:
            out.sort(key=self._timestamp_sort_key)
        except Exception as e:
            logger.error("Failed sorting unified log room messages: %s", e)
            logger.debug("room unified log message sort exception details", exc_info=True)
        return out

    def build_context(
            self,
            *,
            room_id: str,
            limit: int | None = None,
            room_surface: str | None = None,
            room_context_id: str | None = None,
            shared_chat_room_ids: list[str] | None = None,
            excluded_room_modes: set[str] | None = None,
    ) -> str:
        """
        Render prompt-ready room history text from the canonical build_messages() output.
        """
        messages = self.build_messages(
            room_id=room_id,
            limit=limit,
            room_surface=room_surface,
            room_context_id=room_context_id,
            shared_chat_room_ids=shared_chat_room_ids,
            excluded_room_modes=excluded_room_modes,
        )

        if not messages:
            return ""

        lines: list[str] = []
        day_cutoff_utc = day_reset_cutoff_utc()
        old_chat_header_added = False
        today_chat_header_added = False

        for m in messages:
            try:
                content = (getattr(m, "content", None) or "").strip()
                if not content:
                    continue

                sender = (
                        getattr(m, "room_speaker_name", None)
                        or getattr(m, "sender", None)
                        or getattr(m, "role", None)
                        or "Unknown"
                )

                ts = getattr(m, "timestamp", None)
                if ts:
                    try:
                        ts_utc = to_utc(ts)
                        if (
                            ts_utc is not None
                            and ts_utc < day_cutoff_utc
                            and bool(getattr(m, "is_chat", False))
                            and not old_chat_header_added
                        ):
                            lines.append("CHAT FROM YESTERDAY")
                            old_chat_header_added = True
                        if (
                            ts_utc is not None
                            and ts_utc >= day_reset_cutoff_utc
                            and bool(getattr(m, "is_chat", False))
                            and not today_chat_header_added
                        ):
                            lines.append("CHAT FROM TODAY")
                            today_chat_header_added = True
                        local = utc_to_local(ts).strftime("%H:%M")
                        lines.append(f"[{local}] {sender}: {content}")
                        continue
                    except Exception as e:
                        logger.error("Failed formatting room history timestamp: %s", e)
                        logger.debug("room history timestamp format exception details", exc_info=True)

                lines.append(f"{sender}: {content}")
            except Exception as e:
                logger.error("Failed rendering room history message for context: %s", e)
                logger.debug("room history context render exception details", exc_info=True)
                continue

        return "\n".join(lines).strip()

    def build_messages(
            self,
            *,
            room_id: str,
            limit: int | None = None,
            room_surface: str | None = None,
            room_context_id: str | None = None,
            shared_chat_room_ids: list[str] | None = None,
            excluded_room_modes: set[str] | None = None,
    ) -> list:
        # room_id is a case-sensitive identifier (e.g., "slack/C08AB0R54HM");
        # do NOT lowercase it via _normalize_str before the DB lookup.
        rid_target = str(room_id or "").strip()
        if not rid_target:
            return []

        selected = self.load_room_history_messages(
            room_id=rid_target,
            room_surface=room_surface,
            room_context_id=room_context_id,
            shared_chat_room_ids=shared_chat_room_ids,
            excluded_room_modes=excluded_room_modes,
            max_age_hours=24,
        )
        if not selected:
            return []

        candidates: list[Any] = []
        for msg in selected:
            try:
                content = (getattr(msg, "content", None) or "").strip()
                if not content:
                    continue
                candidates.append(msg)
            except Exception as e:
                logger.error("Failed preparing candidate room message for message build: %s", e)
                logger.debug("room message build candidate exception details", exc_info=True)
                continue

        if not candidates:
            return []

        candidates.sort(key=self._timestamp_sort_key, reverse=True)

        kept_messages: list[Any] = []
        kept_ids: set[str] = set()
        day_cutoff_utc = day_reset_cutoff_utc()

        for msg in candidates:
            try:
                ts_utc = to_utc(getattr(msg, "timestamp", None))
                msg_id = str(getattr(msg, "id", "") or "").strip()

                if ts_utc is None:
                    continue

                if ts_utc < day_cutoff_utc:
                    # For previous days, keep only pinned messages or room summaries.
                    # Everything else is hidden from display context.
                    if not self._is_pinned_for_room(msg, rid_target) and not self._is_room_summary_message(msg):
                        continue

                if self._is_suppressed_for_room(msg, rid_target) and not self._is_pinned_for_room(msg, rid_target):
                    continue

                if msg_id and msg_id not in kept_ids:
                    kept_messages.append(msg)
                    kept_ids.add(msg_id)
            except Exception as e:
                logger.error("Failed processing selected room message for message build: %s", e)
                logger.debug("room message build selected processing exception details", exc_info=True)
                continue

        kept_messages.sort(key=self._timestamp_sort_key)

        deduped: list[Any] = []
        seen_ids: set[str] = set()
        seen_fp: set[tuple] = set()

        for msg in kept_messages:
            try:
                msg_id = str(getattr(msg, "id", "") or "").strip()
                fp = self._message_fingerprint(msg)

                if msg_id and msg_id in seen_ids:
                    continue
                if fp in seen_fp:
                    continue

                if msg_id:
                    seen_ids.add(msg_id)
                seen_fp.add(fp)
                deduped.append(msg)
            except Exception:
                logger.debug("Failed deduplicating room history message", exc_info=True)
                continue

        if isinstance(limit, int) and limit > 0 and len(deduped) > limit:
            deduped = deduped[-limit:]

        return deduped

    def _is_dayflow_room(self, room_id: str) -> bool:
        return str(room_id or "").strip().lower() == "dayflow_orchestrator"

    def _is_room_summary_message(self, msg: Any) -> bool:
        sub = set(getattr(msg, "sub_data_type", []) or [])
        if "history_summary" in sub:
            return True

        meta = getattr(msg, "metadata", None)
        meta = meta if isinstance(meta, dict) else {}
        return str(meta.get("summary_kind") or "").strip().lower() == "room_chat_group"

    def _is_foreign_room_summary(self, msg: Any, room_id: str) -> bool:
        if not self._is_room_summary_message(msg):
            return False

        origin_room = self._normalize_str(getattr(msg, "room_id", "") or "")
        consumer_room = self._normalize_str(room_id)
        return bool(origin_room and consumer_room and origin_room != consumer_room)


    def _include_message_for_room_history(self, msg: Any, room_id: str) -> bool:
        if self._is_foreign_room_summary(msg, room_id):
            return False
        # There are rooms that are so special that we hardcode their own logic here.
        if self._is_dayflow_room(room_id):
            return self._include_dayflow_message(msg)
        return bool(getattr(msg, "is_chat", False))


    _DAYFLOW_INCLUDE_TAGS = frozenset({
        "dayflow_chat",
        "dayflow_artifact",
        "dayflow_task",
        "dayflow_action_log",
        "dayflow_synopsis",
    })

    _DAYFLOW_EXCLUDE_TAGS = frozenset({
        "dayflow_system",
    })

    def _include_dayflow_message(self, msg: Any) -> bool:
        sub = set(getattr(msg, "sub_data_type", []) or [])

        meta = getattr(msg, "metadata", None)
        meta = meta if isinstance(meta, dict) else {}
        meta_sub = meta.get("sub_data_type")
        if isinstance(meta_sub, list):
            sub.update(meta_sub)

        if sub & self._DAYFLOW_EXCLUDE_TAGS:
            return False

        if sub & self._DAYFLOW_INCLUDE_TAGS:
            return True

        if "history_summary" in sub:
            return True

        if bool(getattr(msg, "is_chat", False)):
            return True

        return False

    def load_room_history_messages(
            self,
            *,
            room_id: str,
            room_surface: str | None = None,
            room_context_id: str | None = None,
            shared_chat_room_ids: list[str] | None = None,
            excluded_room_modes: set[str] | None = None,
            max_age_hours: int | None = 24,
    ) -> list:
        """
        Shared room-history loader used by both prompt building and room summarization.

        Reads normalized room messages **purely from unified_log_2026**. The
        global_blackboard is no longer consulted — all rooms (master_room,
        slack, telegram, sms, etc.) persist inbound and outbound through
        unified_log first, and that table is the single source of truth.

        Optional age capping applied via max_age_hours; cross-room dedup
        applied downstream.
        """
        # Room/surface/context identifiers are case-sensitive in unified_log_2026
        # (e.g., "slack/C08AB0R54HM"). Don't route them through _normalize_str —
        # that lowercases and would never match the stored rows.
        rid_target = str(room_id or "").strip()
        if not rid_target:
            return []

        surface_target = str(room_surface or "").strip() if room_surface is not None else ""
        ctx_target = str(room_context_id or "").strip() if room_context_id is not None else ""
        excluded_modes = {m.strip().lower() for m in (excluded_room_modes or set()) if m.strip()}

        all_messages = self._load_unified_chat_messages(
            room_id=rid_target,
            room_surface=surface_target if surface_target else None,
            room_context_id=ctx_target if ctx_target else None,
            upper_bound_utc=None,
            shared_room_ids=shared_chat_room_ids or [],
        )

        if isinstance(max_age_hours, int) and max_age_hours > 0:
            cap_cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            capped_messages = []
            for m in all_messages:
                ts_utc = to_utc(getattr(m, "timestamp", None))
                if ts_utc is None:
                    continue

                if ts_utc >= cap_cutoff_utc or self._is_pinned_for_room(m, rid_target):
                    capped_messages.append(m)

            all_messages = capped_messages

        selected = []
        for m in all_messages or []:
            try:
                data = getattr(m, "data", None)
                if isinstance(data, dict) and bool(data.get("room_simulated", False)):
                    continue

                transport_message_id = str(getattr(m, "transport_message_id", "") or "").strip()
                if transport_message_id.startswith("SIM-"):
                    continue

                if not self._include_message_for_room_history(msg=m, room_id=rid_target):
                    continue

                if excluded_modes:
                    meta = getattr(m, "metadata", None)
                    if isinstance(meta, dict):
                        msg_mode = str(meta.get("room_mode") or "").strip().lower()
                        if msg_mode and msg_mode in excluded_modes:
                            continue
                        # Also exclude untagged messages that have mode session IDs
                        # (legacy messages persisted before room_mode tagging was fixed).
                        if not msg_mode:
                            if meta.get("task_creation_session_id") and "task_creation_mode" in excluded_modes:
                                continue
                            if meta.get("doc_creation_session_id") and "doc_creation_mode" in excluded_modes:
                                continue
                            if meta.get("plan_session_id") and "planning_mode" in excluded_modes:
                                continue

                selected.append(m)
            except Exception as e:
                logger.error("Failed filtering a room message for shared room history build: %s", e)
                logger.debug("shared room history filter exception details", exc_info=True)
                continue

        try:
            selected.sort(key=self._timestamp_sort_key)
        except Exception as e:
            logger.error("Failed sorting shared room history messages: %s", e)
            logger.debug("shared room history sort exception details", exc_info=True)

        return selected