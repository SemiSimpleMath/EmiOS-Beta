from __future__ import annotations

from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_ALLOWED_ACTIONS = {
    "get_status",
    "set_mode",
    "set_target_temperature",
    "set_eco_mode",
    "set_fan_mode",
}
_MIN_TARGET_C = 10.0
_MAX_TARGET_C = 32.0
_MIN_INFERRED_C = 15.0
_MAX_INFERRED_C = 30.0
_MIN_INFERRED_F = 50.0
_MAX_INFERRED_F = 80.0


def _coerce_numeric(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be numeric.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field_name} must be numeric.")
        normalized = raw.replace(",", "")
        if normalized.count(".") > 1:
            raise ValueError(f"{field_name} must be numeric.")
        if normalized.startswith("-"):
            normalized_digits = normalized[1:]
        else:
            normalized_digits = normalized
        if not normalized_digits or not normalized_digits.replace(".", "", 1).isdigit():
            raise ValueError(f"{field_name} must be numeric.")
        return float(normalized)
    raise ValueError(f"{field_name} must be numeric.")


def _fahrenheit_to_celsius(value_f: float) -> float:
    return (value_f - 32.0) * (5.0 / 9.0)


def _validate_target_temperature_range_c(value_c: float) -> None:
    if value_c < _MIN_TARGET_C or value_c > _MAX_TARGET_C:
        raise ValueError(
            "target temperature is outside safe range: "
            f"{_MIN_TARGET_C:.1f}C to {_MAX_TARGET_C:.1f}C."
        )


def _resolve_target_temperature_c(arguments: Dict[str, Any]) -> tuple[float, str]:
    generic_keys = ["target_temperature", "temperature", "temp"]
    explicit_c = "target_temperature_c"
    explicit_f = "target_temperature_f"

    provided_keys = []
    for key in [explicit_c, explicit_f, *generic_keys]:
        if key in arguments and arguments.get(key) is not None:
            provided_keys.append(key)

    if not provided_keys:
        raise ValueError(
            "nest_home_control.set_target_temperature requires one temperature value. "
            "Use target_temperature (auto-infers C/F), or provide target_temperature_c or target_temperature_f."
        )
    if len(provided_keys) > 1:
        raise ValueError(
            "nest_home_control.set_target_temperature received multiple temperature fields "
            f"{provided_keys}. Provide exactly one."
        )

    key = provided_keys[0]
    if key == explicit_c:
        target_c = _coerce_numeric(arguments.get(explicit_c), explicit_c)
        return target_c, "C"
    if key == explicit_f:
        target_f = _coerce_numeric(arguments.get(explicit_f), explicit_f)
        return _fahrenheit_to_celsius(target_f), "F"

    raw_temp = _coerce_numeric(arguments.get(key), key)
    if _MIN_INFERRED_F <= raw_temp <= _MAX_INFERRED_F:
        return _fahrenheit_to_celsius(raw_temp), "F"
    if _MIN_INFERRED_C <= raw_temp <= _MAX_INFERRED_C:
        return raw_temp, "C"
    raise ValueError(
        "nest_home_control.set_target_temperature rejected this temperature. "
        "Auto-inference accepts 50-80 as Fahrenheit or 15-30 as Celsius. "
        "Values below 15, between 30 and 50, or above 80 are rejected."
    )


def _validate_action_arguments(action: str, arguments: Dict[str, Any]) -> None:
    if action == "set_mode":
        mode = str(arguments.get("mode") or "").strip().lower()
        if mode not in {"heat", "cool", "heat_cool", "off"}:
            raise ValueError("nest_home_control.set_mode requires mode in: heat|cool|heat_cool|off.")
    elif action == "set_target_temperature":
        target_c, inferred_unit = _resolve_target_temperature_c(arguments)
        # Normalize downstream contract so bridge always receives Celsius.
        arguments["target_temperature_c"] = round(target_c, 2)
        if inferred_unit == "F":
            arguments.pop("target_temperature_f", None)
        arguments.pop("target_temperature", None)
        arguments.pop("temperature", None)
        arguments.pop("temp", None)
        _validate_target_temperature_range_c(target_c)
    elif action == "set_eco_mode":
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("nest_home_control.set_eco_mode requires boolean enabled.")
    elif action == "set_fan_mode":
        fan_mode = str(arguments.get("fan_mode") or "").strip().lower()
        if fan_mode not in {"on", "auto", "off"}:
            raise ValueError("nest_home_control.set_fan_mode requires fan_mode in: on|auto|off.")


class NestHomeControlTool(BaseTool):
    requires_approval = False

    def __init__(self):
        super().__init__("nest_home_control")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}
            action = str(arguments.get("action") or "").strip().lower()
            if action not in _ALLOWED_ACTIONS:
                raise ValueError(
                    "nest_home_control.action must be one of: " + ", ".join(sorted(_ALLOWED_ACTIONS))
                )
            _validate_action_arguments(action, arguments)

            response_data = send_smart_home_command(
                integration="nest",
                action=action,
                arguments=arguments,
                request_id=tool_message.request_id,
            )
            return ToolResult(
                result_type="smart_home",
                content=f"Nest action '{action}' executed successfully.",
                data=response_data,
            )
        except Exception as e:
            logger.error("nest_home_control execution failed: %s", e)
            logger.debug("nest_home_control exception details", exc_info=True)
            return make_tool_error(
                error_code="nest_home_control_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "nest_home_control"},
            )


def get_tool_class():
    return NestHomeControlTool
