from __future__ import annotations

from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_ALLOWED_COMMANDS = {
    "list_lights",
    "set_light_power",
}


_ALL_SYNONYMS = frozenset({"all", "all lights", "everywhere", "whole house", "entire house"})


def _validate_command_arguments(command: str, arguments: Dict[str, Any]) -> None:
    room = arguments.get("room")
    if room is not None and not isinstance(room, str):
        raise ValueError(
            "lights_control.room must be a string when provided. "
            "Do not pass arrays; omit room/light_id to target all lights."
        )
    # Normalize "all" and similar prose values to None (target all lights).
    if isinstance(room, str) and room.strip().lower() in _ALL_SYNONYMS:
        arguments.pop("room", None)
    light_id = arguments.get("light_id")
    if light_id is not None and not isinstance(light_id, str):
        raise ValueError("lights_control.light_id must be a string when provided.")

    if command == "set_light_power":
        state = str(arguments.get("state") or "").strip().lower()
        if state not in {"on", "off"}:
            raise ValueError("lights_control.set_light_power requires state in: on|off.")


class LightsControlTool(BaseTool):
    requires_approval = True

    def __init__(self):
        super().__init__("lights_control")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}
            command = str(arguments.get("command") or "").strip().lower()
            if command not in _ALLOWED_COMMANDS:
                raise ValueError(
                    "lights_control.command must be one of: " + ", ".join(sorted(_ALLOWED_COMMANDS))
                )
            _validate_command_arguments(command, arguments)

            response_data = send_smart_home_command(
                integration="lights",
                command=command,
                arguments=arguments,
                request_id=tool_message.request_id,
            )
            content = f"Lights command '{command}' executed successfully."
            unreachable = (response_data or {}).get("unreachable") if isinstance(response_data, dict) else None
            if unreachable:
                names = ", ".join(
                    str(u.get("alias") or u.get("host") or "unknown") for u in unreachable)
                changed = (response_data or {}).get("changed")
                done = f" on {len(changed)} device(s)" if isinstance(changed, list) else ""
                content = (f"Lights command '{command}' executed{done}. "
                           f"Unreachable (skipped): {names}. The job is done for every "
                           f"reachable light — report the unreachable device, do not retry.")
            return ToolResult(
                result_type="smart_home",
                content=content,
                data=response_data,
            )
        except Exception as e:
            logger.error("lights_control execution failed: %s", e)
            logger.debug("lights_control exception details", exc_info=True)
            return make_tool_error(
                error_code="lights_control_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "lights_control"},
            )


def get_tool_class():
    return LightsControlTool
