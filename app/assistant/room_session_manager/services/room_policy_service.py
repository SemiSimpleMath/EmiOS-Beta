"""
Room policy resolution helpers.

Stateless functions that resolve manager names, persistence modes,
authority levels, and unified-log policy from room context dicts.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from app.assistant.utils.coercion import coerce_authority_level
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def resolve_room_manager_name(room_ctx: Dict[str, Any] | None) -> str:
    if not isinstance(room_ctx, dict):
        return "room_manager"
    raw = room_ctx.get("room_manager_name")
    if not isinstance(raw, str) or not raw.strip():
        return "room_manager"
    manager_name = raw.strip()
    if not re.match(r"^[A-Za-z0-9_:-]+$", manager_name):
        raise ValueError(f"Invalid room manager name in room context: {manager_name!r}")
    return manager_name


def normalize_message_persistence_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    allowed = {"global_blackboard_only", "global_blackboard_and_unified_log"}
    if mode not in allowed:
        raise ValueError(
            f"Invalid message_persistence_mode '{mode}'. "
            "Expected 'global_blackboard_only' or 'global_blackboard_and_unified_log'."
        )
    return mode


def default_authority_for_surface(*, surface: str) -> int:
    from app.assistant.utils.surfaces import DEFAULT_AUTHORITY_BY_SURFACE
    surface_norm = str(surface or "").strip().lower()
    return DEFAULT_AUTHORITY_BY_SURFACE.get(surface_norm, 0)


def resolve_room_authority_level(*, room_policy: dict[str, Any], surface: str) -> int:
    raw = room_policy.get(
        "authority_level",
        default_authority_for_surface(surface=surface),
    )
    return coerce_authority_level(raw, field_name="room_policy.authority_level")


def room_policy_allows_unified_log(room_ctx: Dict[str, Any] | None) -> bool:
    if not isinstance(room_ctx, dict):
        return True
    policy = room_ctx.get("room_policy")
    if not isinstance(policy, dict):
        return True
    candidates = [
        policy.get("write_unified_log"),
        (policy.get("retention") or {}).get("write_unified_log") if isinstance(policy.get("retention"), dict) else None,
        (policy.get("persistence") or {}).get("write_unified_log") if isinstance(policy.get("persistence"), dict) else None,
        (policy.get("ingestion") or {}).get("write_unified_log") if isinstance(policy.get("ingestion"), dict) else None,
    ]
    for val in candidates:
        if isinstance(val, bool):
            return val
    return True


def resolve_room_chat_compaction(room_ctx: Dict[str, Any] | None) -> tuple[bool, str | None]:
    """Resolve whether a room opts into async chat-history compaction and which
    summary agent it uses.

    Per-room and opt-in: a room enables compaction by declaring
    ``chat_compaction: {enabled: true, summary_agent: "<agent>"}`` in its ROOM.md
    policy block. ``summary_agent`` may be a room-specific agent
    (e.g. ``master_room::room_summary``) or the shared generic ``room_summary``.

    Returns ``(enabled, agent)``. Absent/disabled config => ``(False, None)`` and
    the room is never summarized (this is what keeps transient/ephemeral rooms out).
    Raises if a room enables compaction without naming an agent — each room must
    point to its own summarizer rather than inherit a hidden default.
    """
    if not isinstance(room_ctx, dict):
        return False, None
    policy = room_ctx.get("room_policy")
    if not isinstance(policy, dict):
        return False, None
    cfg = policy.get("chat_compaction")
    if not isinstance(cfg, dict):
        return False, None
    if not bool(cfg.get("enabled", False)):
        return False, None
    agent = cfg.get("summary_agent")
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError(
            "room_policy.chat_compaction.enabled is true but summary_agent is missing/empty."
        )
    return True, agent.strip()


def resolve_room_unified_log_persistence(
        *,
        message_persistence_mode: str,
        room_ctx: Dict[str, Any] | None,
        room_id: str,
        room_surface: str,
) -> tuple[bool, str]:
    mode = normalize_message_persistence_mode(message_persistence_mode)
    if mode != "global_blackboard_and_unified_log":
        return False, "mode_blackboard_only"
    if not room_policy_allows_unified_log(room_ctx):
        logger.info(
            "Room unified log persistence blocked by room policy. room_id=%s room_surface=%s",
            room_id,
            room_surface,
        )
        return False, "room_policy_blocked"
    return True, "allowed"
