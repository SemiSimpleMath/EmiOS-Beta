"""
Room policy resolution helpers.

Stateless functions that resolve manager names, persistence modes,
authority levels, and unified-log policy from room context dicts.
"""
from __future__ import annotations

import re
from typing import Any, Dict

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


def coerce_authority_level(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    try:
        level = int(value)
    except Exception:
        raise ValueError(f"{field_name} must be an integer between 0 and 100.")
    if level < 0 or level > 100:
        raise ValueError(f"{field_name} must be between 0 and 100.")
    return level


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
