from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Set, Tuple

from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import to_utc


DEFAULT_EXCLUDED_CHAT_TAGS: Set[str] = {
    "history_summary",
    "entity_card_injection",
    "agent_notification",
    "agent_guard",
    "slash_command",
    "doc_creation_mode",
}

# Room modes that are NOT normal chat.  Messages tagged with these modes
# are excluded from cross-room chat retrieval by default.
_NON_NORMAL_ROOM_MODES: frozenset[str] = frozenset({
    "task_creation_mode",
    "doc_creation_mode",
    "planning_mode",
    "game_mode",
})


def normalize_room_scope_filters(
    *,
    room_id: Optional[str],
    shared_room_ids: Optional[Iterable[str]],
    room_surface: Optional[str],
    room_context_id: Optional[str],
) -> Tuple[Set[str], Optional[str], Optional[str]]:
    scoped_room_id = str(room_id).strip() if isinstance(room_id, str) else None
    allowed_room_ids: Set[str] = set()
    if scoped_room_id:
        allowed_room_ids.add(scoped_room_id)
    if shared_room_ids is not None:
        for rid in shared_room_ids:
            if isinstance(rid, str) and rid.strip():
                allowed_room_ids.add(rid.strip())

    scoped_room_surface = str(room_surface).strip() if isinstance(room_surface, str) else None
    scoped_room_context_id = str(room_context_id).strip() if isinstance(room_context_id, str) else None
    if scoped_room_surface and scoped_room_surface.lower() == "any":
        scoped_room_surface = None
    if scoped_room_context_id and scoped_room_context_id.lower() == "any":
        scoped_room_context_id = None
    return allowed_room_ids, scoped_room_surface, scoped_room_context_id


def should_include_chat_message(
    *,
    msg: Message,
    cutoff_utc: datetime,
    allowed_room_ids: Set[str],
    room_surface: Optional[str],
    room_context_id: Optional[str],
    include_tags: Set[str],
    exclude_tags: Set[str],
    include_command_scopes: Set[str],
    include_summarized: bool,
) -> bool:
    if not bool(getattr(msg, "is_chat", False)):
        return False

    if allowed_room_ids:
        msg_room_id = str(getattr(msg, "room_id", "") or "").strip()
        if msg_room_id not in allowed_room_ids:
            return False
    if room_surface is not None:
        msg_room_surface = str(getattr(msg, "room_surface", "") or "").strip()
        if msg_room_surface != room_surface:
            return False
    if room_context_id is not None:
        msg_room_context_id = str(getattr(msg, "room_context_id", "") or "").strip()
        if msg_room_context_id != room_context_id:
            return False

    tags = set(getattr(msg, "sub_data_type", []) or [])
    if "slash_command" in tags and include_command_scopes:
        meta = getattr(msg, "metadata", None)
        scope = meta.get("command_scope") if isinstance(meta, dict) else None
        if scope in include_command_scopes:
            tags.discard("slash_command")
        else:
            return False

    if include_tags and not tags.intersection(include_tags):
        return False
    if tags.intersection(exclude_tags):
        return False

    if not include_summarized:
        meta = getattr(msg, "metadata", None)
        if isinstance(meta, dict) and bool(meta.get("summarized", False)):
            return False

    msg_ts_utc = to_utc(getattr(msg, "timestamp", None))
    if msg_ts_utc is None or msg_ts_utc <= cutoff_utc:
        return False
    return True
