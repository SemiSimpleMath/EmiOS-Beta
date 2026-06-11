from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.assistant.routine_manager.utils import read_json_file, status_dir

CONTROL_RESOURCE_FILENAME = "resource_screen_capture_control.json"


def control_path() -> Path:
    return status_dir() / CONTROL_RESOURCE_FILENAME


def default_control() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": False,
        "enabled_at_utc": None,
        "enabled_by": None,
        "disabled_at_utc": None,
        "disabled_by": None,
        "disabled_reason": None,
        # Last local date (YYYY-MM-DD) when we auto-disabled at 08:30.
        "auto_disabled_date_local": None,
    }


def load_control() -> Dict[str, Any]:
    data = read_json_file(control_path()) or {}
    if not isinstance(data, dict):
        data = {}
    merged = default_control()
    merged.update(data)
    return merged
