from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.routine_manager.utils import read_json_file, status_dir
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

CONTROL_RESOURCE_FILENAME = "resource_screen_capture_control.json"


def _control_path() -> Path:
    return status_dir() / CONTROL_RESOURCE_FILENAME


def _default_control() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": False,
        "enabled_at_utc": None,
        "enabled_by": None,
        "disabled_at_utc": None,
        "disabled_by": None,
        "disabled_reason": None,
        "auto_disabled_date_local": None,
    }


def _load_control() -> Dict[str, Any]:
    data = read_json_file(_control_path()) or {}
    if not isinstance(data, dict):
        data = {}
    merged = _default_control()
    merged.update(data)
    return merged


class SetScreenCaptureEnabledTool(BaseTool):
    def __init__(self):
        super().__init__("set_screen_capture_enabled")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        enabled = bool(args.get("enabled", False))
        actor = str(args.get("actor") or "user").strip() or "user"
        reason = str(args.get("reason") or "").strip() or None

        control = _load_control()
        now_utc_iso = datetime.now(timezone.utc).isoformat()

        if enabled:
            control["enabled"] = True
            control["enabled_at_utc"] = now_utc_iso
            control["enabled_by"] = actor
            control["disabled_at_utc"] = None
            control["disabled_by"] = None
            control["disabled_reason"] = None
        else:
            control["enabled"] = False
            control["disabled_at_utc"] = now_utc_iso
            control["disabled_by"] = actor
            control["disabled_reason"] = reason or "manual_off"

        write_json_atomic(_control_path(), control)
        logger.info("Screen capture toggle updated: enabled=%s by=%s", enabled, actor)

        return ToolResult(
            result_type="tool_result",
            content=f"screen_capture enabled={enabled}",
            data={"control_path": str(_control_path()), "control": control},
        )


def get_tool_class():
    return SetScreenCaptureEnabledTool

