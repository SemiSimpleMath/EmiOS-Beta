"""
Room metadata repair service.

Ensures every chat message emitted during a room request carries
room_id, room_surface, and room_context_id.  Catches legacy/global
add_msg paths that omitted room fields.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_REPAIR_COUNTS: dict[str, int] = {}
_REPAIR_LOCK = threading.Lock()


def metadata_repair_strict_enabled() -> bool:
    explicit = str(os.environ.get("EMI_STRICT_ROOM_METADATA_REPAIR") or "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    env_name = str(os.environ.get("APP_ENV") or os.environ.get("ENV") or "").strip().lower()
    if env_name in {"dev", "development", "test", "testing"}:
        return True
    if str(os.environ.get("PYTEST_CURRENT_TEST") or "").strip():
        return True
    return False


def ensure_request_room_metadata(
        *,
        blackboard: Any,
        request_id: str,
        room_id: str,
        room_surface: str,
        room_context_id: str,
        manager_name: str,
) -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        return
    try:
        all_messages = blackboard.get_messages()
    except Exception:
        logger.debug("room metadata repair: could not get messages", exc_info=True)
        return

    repaired_count = 0
    repaired_message_ids: list[str] = []
    strict_mode = metadata_repair_strict_enabled()
    for m in all_messages or []:
        try:
            if str(getattr(m, "request_id", "") or "").strip() != request_id:
                continue
            if not bool(getattr(m, "is_chat", False)):
                continue

            touched = False
            if not (isinstance(getattr(m, "room_id", None), str) and str(getattr(m, "room_id")).strip()):
                m.room_id = room_id
                repaired_count += 1
                touched = True
            if not (isinstance(getattr(m, "room_surface", None), str) and str(getattr(m, "room_surface")).strip()):
                m.room_surface = room_surface
                repaired_count += 1
                touched = True
            if not (isinstance(getattr(m, "room_context_id", None), str) and str(getattr(m, "room_context_id")).strip()):
                m.room_context_id = room_context_id
                repaired_count += 1
                touched = True

            data = getattr(m, "data", None)
            if not isinstance(data, dict):
                data = {}
            data.setdefault("room_id", room_id)
            data.setdefault("room_surface", room_surface)
            data.setdefault("room_context_id", room_context_id)
            m.data = data
            if touched:
                repaired_message_ids.append(str(getattr(m, "id", "") or "unknown"))
        except Exception:
            logger.debug("room metadata repair: error repairing a message", exc_info=True)
            continue

    if repaired_count > 0:
        counter_key = f"{room_surface}::{manager_name}"
        with _REPAIR_LOCK:
            _REPAIR_COUNTS[counter_key] = int(_REPAIR_COUNTS.get(counter_key, 0)) + int(repaired_count)
            counter_value = int(_REPAIR_COUNTS[counter_key])
        logger.warning(
            "Room metadata repair applied request_id=%s room_id=%s manager=%s repaired_fields=%d "
            "repair_counter=%d message_ids=%s",
            request_id,
            room_id,
            manager_name,
            repaired_count,
            counter_value,
            repaired_message_ids,
        )
        if strict_mode:
            raise RuntimeError(
                f"Room metadata repair strict mode violation request_id={request_id} room_id={room_id} "
                f"repaired_fields={repaired_count}"
            )
