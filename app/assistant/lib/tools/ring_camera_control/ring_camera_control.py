from __future__ import annotations

from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_ALLOWED_COMMANDS = {
    "list_cameras",
    "get_snapshot",
    "get_recent_events",
    "set_siren",
    "set_light",
}


def _validate_command_arguments(command: str, arguments: Dict[str, Any]) -> None:
    if command in {"get_snapshot", "get_recent_events", "set_siren", "set_light"}:
        camera_id = str(arguments.get("camera_id") or "").strip()
        if not camera_id:
            raise ValueError(f"ring_camera_control.{command} requires non-empty camera_id.")
    if command in {"set_siren", "set_light"}:
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"ring_camera_control.{command} requires boolean enabled.")
    if command == "get_recent_events":
        lookback_minutes = arguments.get("lookback_minutes")
        if lookback_minutes is not None:
            try:
                value = int(lookback_minutes)
            except Exception:
                raise ValueError("lookback_minutes must be an integer when provided.")
            if value <= 0:
                raise ValueError("lookback_minutes must be > 0 when provided.")


class RingCameraControlTool(BaseTool):
    requires_approval = True

    def __init__(self):
        super().__init__("ring_camera_control")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}
            command = str(arguments.get("command") or "").strip().lower()
            if command not in _ALLOWED_COMMANDS:
                raise ValueError(
                    "ring_camera_control.command must be one of: " + ", ".join(sorted(_ALLOWED_COMMANDS))
                )
            _validate_command_arguments(command, arguments)

            response_data = send_smart_home_command(
                integration="ring",
                command=command,
                arguments=arguments,
                request_id=tool_message.request_id,
            )
            return ToolResult(
                result_type="smart_home",
                content=f"Ring camera command '{command}' executed successfully.",
                data=response_data,
            )
        except Exception as e:
            logger.error("ring_camera_control execution failed: %s", e)
            logger.debug("ring_camera_control exception details", exc_info=True)
            return make_tool_error(
                error_code="ring_camera_control_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "ring_camera_control"},
            )


def get_tool_class():
    return RingCameraControlTool
