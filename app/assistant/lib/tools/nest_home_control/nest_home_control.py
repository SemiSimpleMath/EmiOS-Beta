from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

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


def _resolve_target_temperature_c(value: Any) -> float:
    raw = _coerce_numeric(value, "target_temperature")
    if _MIN_INFERRED_F <= raw <= _MAX_INFERRED_F:
        target_c = _fahrenheit_to_celsius(raw)
    elif _MIN_INFERRED_C <= raw <= _MAX_INFERRED_C:
        target_c = raw
    else:
        raise ValueError(
            "target_temperature outside accepted range. "
            "Auto-inference accepts 50-80 as Fahrenheit or 15-30 as Celsius."
        )
    if target_c < _MIN_TARGET_C or target_c > _MAX_TARGET_C:
        raise ValueError(
            f"target temperature outside safe range: {_MIN_TARGET_C:.1f}C to {_MAX_TARGET_C:.1f}C."
        )
    return round(target_c, 2)


def _build_operations(arguments: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Decompose a combined-intent payload into one bridge call per state field.

    Stable order: mode → target_temperature → fan_mode → eco_enabled. Mode goes
    first so a follow-on temperature applies under the freshly-selected mode.
    """
    ops: List[Tuple[str, Dict[str, Any]]] = []
    device_id = arguments.get("device_id")

    if arguments.get("mode") is not None:
        mode = str(arguments.get("mode") or "").strip().lower().replace("-", "_")
        if mode not in {"heat", "cool", "heat_cool", "heatcool", "off"}:
            raise ValueError("mode must be one of: HEAT, COOL, HEATCOOL, OFF.")
        if mode == "heatcool":
            mode = "heat_cool"
        op_args: Dict[str, Any] = {"mode": mode}
        if device_id is not None:
            op_args["device_id"] = device_id
        ops.append(("set_mode", op_args))

    if arguments.get("target_temperature") is not None:
        target_c = _resolve_target_temperature_c(arguments.get("target_temperature"))
        op_args = {"target_temperature_c": target_c}
        if device_id is not None:
            op_args["device_id"] = device_id
        ops.append(("set_target_temperature", op_args))

    if arguments.get("fan_mode") is not None:
        fan_mode = str(arguments.get("fan_mode") or "").strip().lower()
        if fan_mode not in {"on", "auto", "off"}:
            raise ValueError("fan_mode must be one of: ON, AUTO, OFF.")
        op_args = {"fan_mode": fan_mode}
        if device_id is not None:
            op_args["device_id"] = device_id
        ops.append(("set_fan_mode", op_args))

    if arguments.get("eco_enabled") is not None:
        if not isinstance(arguments.get("eco_enabled"), bool):
            raise ValueError("eco_enabled must be a boolean.")
        op_args = {"enabled": arguments.get("eco_enabled")}
        if device_id is not None:
            op_args["device_id"] = device_id
        ops.append(("set_eco_mode", op_args))

    return ops


class NestHomeControlTool(BaseTool):
    requires_approval = False

    def __init__(self):
        super().__init__("nest_home_control")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}

            # Read path: explicit get_status flag, OR no state fields at all.
            state_keys = ("mode", "target_temperature", "fan_mode", "eco_enabled")
            has_state = any(arguments.get(k) is not None for k in state_keys)
            if arguments.get("get_status") is True or not has_state:
                read_args: Dict[str, Any] = {}
                if arguments.get("device_id") is not None:
                    read_args["device_id"] = arguments["device_id"]
                response = send_smart_home_command(
                    integration="nest",
                    action="get_status",
                    arguments=read_args,
                    request_id=tool_message.request_id,
                )
                return ToolResult(
                    result_type="smart_home",
                    content="Nest get_status executed successfully.",
                    data=response,
                )

            ops = _build_operations(arguments)
            if not ops:
                raise ValueError("nest_home_control: no recognized state fields supplied.")

            applied: List[str] = []
            responses: List[Dict[str, Any]] = []
            for action, op_args in ops:
                response = send_smart_home_command(
                    integration="nest",
                    action=action,
                    arguments=op_args,
                    request_id=tool_message.request_id,
                )
                applied.append(action)
                responses.append({"action": action, "response": response})

            return ToolResult(
                result_type="smart_home",
                content=f"Nest applied: {', '.join(applied)}.",
                data={"operations": responses},
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
