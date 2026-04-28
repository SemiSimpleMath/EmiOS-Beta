from __future__ import annotations

import re
from pathlib import Path

from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
from app.assistant.utils.identity_names import (
    get_required_primary_user_name,
    resolve_display_name,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_SAFE_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_TEMPLATE_FILES = (
    "resource_identity.json",
    "resource_room_context.json",
    "resource_conversation.json",
    "resource_safety.json",
    "resource_participant_facts.json",
    "policy.json",
    "permissions.json",
    "access.json",
)
_TELEGRAM_TEMPLATE_DIR = Path(__file__).resolve().parent / "_templates" / "telegram_standard"
_SLACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "_templates" / "slack_standard"


def make_telegram_room_id(chat_id: str) -> str:
    raw = str(chat_id or "").strip()
    if not raw:
        raise ValueError("chat_id is required to build telegram room id.")
    candidate = f"telegram/{raw}"
    safe = "".join(ch if re.match(r"[A-Za-z0-9._:-]", ch) else "_" for ch in candidate)
    safe = safe.replace("telegram_", "telegram/", 1)
    if not safe:
        raise ValueError(f"Invalid derived telegram room id from chat_id={chat_id!r}")
    _validate_room_id_or_raise(safe)
    return safe


def make_slack_room_id(channel_id: str) -> str:
    raw = str(channel_id or "").strip()
    if not raw:
        raise ValueError("channel_id is required to build slack room id.")
    candidate = f"slack/{raw}"
    safe = "".join(ch if re.match(r"[A-Za-z0-9._:-]", ch) else "_" for ch in candidate)
    safe = safe.replace("slack_", "slack/", 1)
    if not safe:
        raise ValueError(f"Invalid derived slack room id from channel_id={channel_id!r}")
    _validate_room_id_or_raise(safe)
    return safe


def _validate_room_id_or_raise(room_id: str) -> None:
    rid = str(room_id or "").strip()
    if not rid:
        raise ValueError("room_id is required.")
    if not _SAFE_ROOM_ID_RE.match(rid):
        raise ValueError(f"Invalid room_id={rid!r}")
    parts = rid.split("/")
    if any((not p or p in {".", ".."}) for p in parts):
        raise ValueError(f"Invalid room_id path segments: {rid!r}")


def _ensure_surface_room(
    *,
    room_id: str,
    surface: str,
    template_dir: Path,
    external_id: str,
    sender_display_name: str = "",
) -> Path:
    rid = str(room_id or "").strip()
    _validate_room_id_or_raise(rid)

    base_dir = Path(__file__).resolve().parent
    room_dir = base_dir.joinpath(*rid.split("/"))
    room_dir.mkdir(parents=True, exist_ok=True)

    display_name = resolve_display_name(
        raw_name=str(sender_display_name or "").strip(),
        role="participant",
        external_id=str(external_id or "").strip(),
        prefer_external_id_for_participant=True,
    )
    if not template_dir.exists() or not template_dir.is_dir():
        raise FileNotFoundError(f"Missing room template directory: {template_dir}")

    primary_user = get_required_primary_user_name()
    replacements = {
        "{{ROOM_ID}}": rid,
        "{{SURFACE}}": str(surface or "").strip(),
        "{{EXTERNAL_ID}}": str(external_id or "").strip(),
        "{{CHAT_ID}}": str(external_id or "").strip(),
        "{{CHANNEL_ID}}": str(external_id or "").strip(),
        "{{DISPLAY_NAME}}": display_name,
        "{{PRIMARY_USER_NAME}}": primary_user,
    }

    created_files: list[str] = []
    for name in _TEMPLATE_FILES:
        template_path = template_dir / name
        if not template_path.exists() or not template_path.is_file():
            raise FileNotFoundError(f"Missing required room template file: {template_path}")
        content = template_path.read_text(encoding="utf-8")
        for token, value in replacements.items():
            content = content.replace(token, value)
        path = room_dir / name
        if path.exists():
            continue
        path.write_text(content, encoding="utf-8")
        created_files.append(name)

    _ = load_room_context_for_manager(rid)
    if created_files:
        logger.info("Room provisioned. room_id=%s files_created=%s", rid, created_files)
    return room_dir


def ensure_telegram_room(room_id: str, *, chat_id: str, sender_username: str = "") -> Path:
    return _ensure_surface_room(
        room_id=room_id,
        surface="telegram",
        template_dir=_TELEGRAM_TEMPLATE_DIR,
        external_id=str(chat_id or "").strip(),
        sender_display_name=str(sender_username or "").strip(),
    )


def ensure_slack_room(room_id: str, *, channel_id: str, sender_name: str = "") -> Path:
    return _ensure_surface_room(
        room_id=room_id,
        surface="slack",
        template_dir=_SLACK_TEMPLATE_DIR,
        external_id=str(channel_id or "").strip(),
        sender_display_name=str(sender_name or "").strip(),
    )

