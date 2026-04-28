from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_CACHE: Dict[str, Any] = {"mtime": None, "data": None}


def _default_config() -> Dict[str, Any]:
    return {
        "use_room_mode": False,
        "send_reply": False,
        "allow_real_slack_send": False,
        "message_persistence_mode": "global_blackboard_only",
        "channel_id": "",
        "default_room_id": "",
        "poll_limit": 100,
        "poll_overlap_seconds": 120,
        "status_resource_file": "resources/status/resource_slack_room_status.json",
        "room_name_map": {},
    }


def _config_path() -> Path:
    return get_repo_root() / "configs" / "slack_room.json"


def get_slack_room_config(*, force_reload: bool = False) -> Dict[str, Any]:
    path = _config_path()
    defaults = _default_config()
    if not path.exists():
        return defaults

    try:
        mtime = path.stat().st_mtime
    except Exception:
        return defaults

    if not force_reload and _CACHE.get("mtime") == mtime and isinstance(_CACHE.get("data"), dict):
        return dict(_CACHE["data"])

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("slack_room config is not an object. path=%s", path)
            data = {}
    except Exception as e:
        logger.warning("Failed reading slack_room config: %s (path=%s)", e, path)
        data = {}

    merged = dict(defaults)
    merged.update(data)

    # Normalize room map
    room_name_map = merged.get("room_name_map")
    if not isinstance(room_name_map, dict):
        room_name_map = defaults["room_name_map"]
    normalized_map: Dict[str, str] = {}
    for k, v in room_name_map.items():
        try:
            key = str(k).strip().lower()
            val = str(v).strip()
            if key and val:
                normalized_map[key] = val
        except Exception:
            continue
    merged["room_name_map"] = normalized_map or dict(defaults["room_name_map"])

    # Numeric normalization
    try:
        merged["poll_limit"] = int(merged.get("poll_limit", defaults["poll_limit"]))
    except Exception:
        merged["poll_limit"] = defaults["poll_limit"]
    if merged["poll_limit"] <= 0:
        merged["poll_limit"] = defaults["poll_limit"]

    try:
        merged["poll_overlap_seconds"] = int(merged.get("poll_overlap_seconds", defaults["poll_overlap_seconds"]))
    except Exception:
        merged["poll_overlap_seconds"] = defaults["poll_overlap_seconds"]
    if merged["poll_overlap_seconds"] < 0:
        merged["poll_overlap_seconds"] = defaults["poll_overlap_seconds"]

    status_file = str(merged.get("status_resource_file") or "").strip()
    merged["status_resource_file"] = status_file or defaults["status_resource_file"]
    merged["channel_id"] = str(merged.get("channel_id") or defaults["channel_id"]).strip() or defaults["channel_id"]
    merged["default_room_id"] = (
        str(merged.get("default_room_id") or defaults["default_room_id"]).strip() or defaults["default_room_id"]
    )
    mode = str(merged.get("message_persistence_mode") or defaults["message_persistence_mode"]).strip().lower()
    allowed_modes = {"global_blackboard_only", "global_blackboard_and_unified_log"}
    if mode not in allowed_modes:
        logger.warning(
            "Invalid slack_room message_persistence_mode '%s'. Falling back to '%s'.",
            mode,
            defaults["message_persistence_mode"],
        )
        mode = defaults["message_persistence_mode"]
    merged["message_persistence_mode"] = mode

    _CACHE["mtime"] = mtime
    _CACHE["data"] = merged
    return dict(merged)


def get_slack_channel_id() -> str:
    """
    Resolve Slack channel id with env-first fallback chain.
    """
    for key in ("SLACK_CHANNEL_ID", "EMI_SLACK_CHANNEL_ID", "SLACK_ROOM_CHANNEL_ID"):
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val
    cfg = get_slack_room_config()
    val = str(cfg.get("channel_id") or "").strip()
    return val


def get_slack_default_room_id() -> str:
    """
    Resolve default room id with env-first fallback chain.
    """
    for key in ("EMI_SLACK_DEFAULT_ROOM_ID", "SLACK_DEFAULT_ROOM_ID"):
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val
    cfg = get_slack_room_config()
    val = str(cfg.get("default_room_id") or "").strip()
    return val

