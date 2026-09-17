"""Persist Claude Code session_ids per room context for ``--resume``-based multi-turn.

Single-file JSON store at ``data/emi_code/sessions.json`` keyed by
``(room_id, room_context_id)`` so each EmiCode conversation has its own
ongoing session. Cleared by deleting the entry (e.g. ``/clear`` slash
command in v2) or by the user resetting the room.

Simple to debug, simple to wipe. If the file is missing or corrupt we
just start fresh — no migrations, no indices, no thread safety beyond
the GIL (single-process Flask).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


def _store_path() -> Path:
    return get_repo_root() / "data" / "emi_code" / "sessions.json"


def _load() -> dict:
    p = _store_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[emi_code session_store] failed to load %s: %s", p, exc)
        return {}


def _save(data: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _key(room_id: str, room_context_id: str) -> str:
    return f"{room_id}::{room_context_id or 'main'}"


def get_session_id(room_id: str, room_context_id: str = "main") -> Optional[str]:
    data = _load()
    val = data.get(_key(room_id, room_context_id))
    return str(val).strip() if val else None


def set_session_id(room_id: str, room_context_id: str, session_id: str) -> None:
    if not session_id:
        return
    data = _load()
    data[_key(room_id, room_context_id)] = session_id
    _save(data)


def clear_session(room_id: str, room_context_id: str = "main") -> None:
    data = _load()
    key = _key(room_id, room_context_id)
    if key in data:
        del data[key]
        _save(data)
