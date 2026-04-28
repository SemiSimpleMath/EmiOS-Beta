"""Ingest cross-room chat into dayflow items.

Reads chat messages from the unified log for rooms the dayflow is entitled
to (via ``chat_ingestion_entitled_rooms`` in access.json; falls back to
``shared_chat_room_ids``), converts each to a
standard dayflow item Message, and returns them for persistence.

The caller is responsible for:
  1. Providing the watermark (``since_utc``) so only new messages are loaded.
  2. Filtering out item IDs that already exist in the dayflow state store.
  3. Persisting the returned Messages via ``persist_dayflow_items()``.
  4. Advancing the watermark after successful persistence.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import or_, select

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.utils.identity_names import (
    get_required_assistant_name,
    get_required_primary_user_name,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

DAYFLOW_ROOM_ID = "dayflow_orchestrator"

_CHAT_SOURCES = frozenset({
    "chat",
    "room_ui",
    "room_sms",
    "room_telegram",
    "room_slack",
    "slack",
})

def _build_chat_item_id(origin_room: str, source_msg_id: str) -> str:
    seed = f"{origin_room}|{source_msg_id}".strip().lower()
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"chat:{digest}"


def _resolve_user_display_name() -> str:
    """Resolve the primary user's display name from shared identity resolver."""
    return get_required_primary_user_name()


_EXCLUDED_CONTENT_TYPES = frozenset({
    "ticket",
    "ticket_response",
    "ticket_action",
    "suggestion",
    "notification",
    "system",
})


def _is_ticket_noise(row: UnifiedLog2026) -> bool:
    """Return True if the row is ticket/system noise rather than real conversation."""
    ct = str(row.content_type or "").strip().lower()
    if ct in _EXCLUDED_CONTENT_TYPES:
        return True
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    if meta.get("ticket_id") or meta.get("suggestion_type"):
        return True
    return False


from app.assistant.utils.message_visibility_policy import _NON_NORMAL_ROOM_MODES


def _is_non_normal_mode(row: UnifiedLog2026) -> bool:
    """Return True if the message is from a non-normal room mode (task creation, doc, etc.)."""
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    mode = str(meta.get("room_mode") or "").strip().lower()
    if mode in _NON_NORMAL_ROOM_MODES:
        return True
    # Also catch untagged legacy messages that have mode session IDs.
    if not mode and (meta.get("task_creation_session_id") or meta.get("doc_creation_session_id") or meta.get("plan_session_id")):
        return True
    return False


def _load_chat_since(
    *,
    room_ids: list[str],
    since_utc: datetime,
    sql_limit: int = 500,
) -> list[UnifiedLog2026]:
    if not room_ids:
        return []

    try:
        with get_db_manager().read_session() as session:
            stmt = (
                select(UnifiedLog2026)
                .where(UnifiedLog2026.source.in_(list(_CHAT_SOURCES)))
                .where(UnifiedLog2026.timestamp > since_utc)
                .where(or_(
                    UnifiedLog2026.direction == "inbound",
                    UnifiedLog2026.direction == "outbound",
                    UnifiedLog2026.direction.is_(None),
                ))
                .order_by(UnifiedLog2026.timestamp.asc())
                .limit(max(int(sql_limit or 0), 1))
            )

            if len(room_ids) == 1:
                stmt = stmt.where(UnifiedLog2026.room_id == room_ids[0])
            else:
                stmt = stmt.where(UnifiedLog2026.room_id.in_(room_ids))

            rows = session.execute(stmt).scalars().all()
            return [r for r in rows if not _is_ticket_noise(r) and not _is_non_normal_mode(r)]
    except Exception as e:
        logger.error("chat_ingestion: failed loading chat rows: %s", e)
        logger.debug("chat_ingestion load exception details", exc_info=True)
        raise


def _row_to_dayflow_message(row: UnifiedLog2026, *, now_utc: datetime) -> Message:
    source_msg_id = str(row.id or "").strip()
    if not source_msg_id:
        raise ValueError("chat_ingestion: unified log row has no id.")

    origin_room = str(row.room_id or "").strip()
    direction = str(row.direction or "").strip().lower()
    role = str(row.role or "").strip().lower()
    speaker_role = str(row.speaker_role or "").strip().lower()
    raw_name = str(row.speaker_name or "").strip()
    if raw_name:
        sender = raw_name
    else:
        effective_role = role or speaker_role
        if effective_role in {"assistant", get_required_assistant_name().lower(), "agent"}:
            sender = get_required_assistant_name()
        elif effective_role in {"user", "human", "owner", "participant"}:
            sender = _resolve_user_display_name()
        elif direction == "outbound":
            sender = get_required_assistant_name()
        else:
            sender = _resolve_user_display_name()
    content = str(row.message or "").strip()
    if not content:
        raise ValueError(f"chat_ingestion: row {source_msg_id} has empty content.")

    ts = row.timestamp
    if ts is None:
        raise ValueError(f"chat_ingestion: row {source_msg_id} has no timestamp.")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    item_id = _build_chat_item_id(origin_room, source_msg_id)
    summary = f"{sender}: {content}"

    from app.assistant.utils.time_utils import get_local_timezone
    local_tz = get_local_timezone()
    ts_local = ts.astimezone(local_tz)
    created_at_local = ts_local.strftime("%I:%M %p")

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "source_type": "chat",
        "event_type": "cross_room_chat",
        "created_at": ts.isoformat(),
        "created_at_local": created_at_local,
        "summary": summary,
        "importance": "low",
        "actionability": "context_only",
        "state": "artifact",
        "state_reason": "chat_context",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
        "chat_origin_room_id": origin_room,
        "chat_source_message_id": source_msg_id,
        "chat_sender": sender,
        "chat_direction": direction,
        "chat_role": role,
    }

    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "chat", "dayflow_chat"],
        sender=sender,
        content=summary,
        timestamp=ts,
        room_id=DAYFLOW_ROOM_ID,
        metadata=metadata,
    )


def ingest_cross_room_chat(
    *,
    since_utc: datetime,
    entitled_room_ids: list[str],
    now_utc: datetime | None = None,
) -> tuple[list[Message], datetime | None]:
    """Load new cross-room chat and convert to dayflow items.

    Returns ``(messages, new_watermark)`` where *new_watermark* is the
    timestamp of the newest ingested message (or ``None`` when nothing
    was ingested).  The caller should persist the watermark after
    successfully persisting the messages.
    """
    now = now_utc or datetime.now(timezone.utc)

    source_room_ids = [r.strip() for r in entitled_room_ids if r.strip()]
    if not source_room_ids:
        logger.debug("chat_ingestion: no entitled room IDs, skipping.")
        return [], None

    rows = _load_chat_since(room_ids=source_room_ids, since_utc=since_utc)
    if not rows:
        logger.debug("chat_ingestion: no new chat since %s.", since_utc.isoformat())
        return [], None

    messages: list[Message] = []
    newest_ts: datetime | None = None

    for row in rows:
        try:
            msg = _row_to_dayflow_message(row, now_utc=now)
            messages.append(msg)

            row_ts = row.timestamp
            if row_ts is not None:
                if row_ts.tzinfo is None:
                    row_ts = row_ts.replace(tzinfo=timezone.utc)
                if newest_ts is None or row_ts > newest_ts:
                    newest_ts = row_ts
        except Exception as e:
            logger.error("chat_ingestion: skipping row %s: %s", getattr(row, "id", "?"), e)
            logger.debug("chat_ingestion row conversion exception details", exc_info=True)
            continue

    logger.info(
        "chat_ingestion: converted %d/%d chat row(s) from rooms %s since %s.",
        len(messages),
        len(rows),
        source_room_ids,
        since_utc.isoformat(),
    )
    return messages, newest_ts
