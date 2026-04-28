from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any, Dict, List

import requests
from flask import Blueprint, jsonify, request

from app.assistant.lib.google_auth.account_ids import NEST_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.google_credentials import load_google_credentials
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir

logger = get_logger(__name__)

smart_home_bridge_bp = Blueprint("smart_home_bridge", __name__)

_SDM_BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
_NEST_REQUIRED_SCOPES = ["https://www.googleapis.com/auth/sdm.service"]
_NEST_MIN_TARGET_C = 10.0
_NEST_MAX_TARGET_C = 32.0
_NEST_MIN_INFERRED_C = 15.0
_NEST_MAX_INFERRED_C = 30.0
_NEST_MIN_INFERRED_F = 50.0
_NEST_MAX_INFERRED_F = 80.0


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


def _validate_nest_target_temperature_c(value_c: float) -> None:
    if value_c < _NEST_MIN_TARGET_C or value_c > _NEST_MAX_TARGET_C:
        raise ValueError(
            "Nest target temperature outside safe range: "
            f"{_NEST_MIN_TARGET_C:.1f}C to {_NEST_MAX_TARGET_C:.1f}C."
        )


def _resolve_nest_target_temperature_c(arguments: Dict[str, Any]) -> tuple[float, str]:
    generic_keys = ["target_temperature", "temperature", "temp"]
    explicit_c = "target_temperature_c"
    explicit_f = "target_temperature_f"

    provided_keys = []
    for key in [explicit_c, explicit_f, *generic_keys]:
        if key in arguments and arguments.get(key) is not None:
            provided_keys.append(key)
    if not provided_keys:
        raise ValueError(
            "Nest set_target_temperature requires one temperature value. "
            "Use target_temperature (auto-infers C/F), or provide target_temperature_c or target_temperature_f."
        )
    if len(provided_keys) > 1:
        raise ValueError(
            "Nest set_target_temperature received multiple temperature fields "
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
    if _NEST_MIN_INFERRED_F <= raw_temp <= _NEST_MAX_INFERRED_F:
        return _fahrenheit_to_celsius(raw_temp), "F"
    if _NEST_MIN_INFERRED_C <= raw_temp <= _NEST_MAX_INFERRED_C:
        return raw_temp, "C"
    raise ValueError(
        "Nest target temperature is outside supported inferred ranges. "
        "Auto-inference accepts 50-80 as Fahrenheit or 15-30 as Celsius. "
        "Values below 15, between 30 and 50, or above 80 are rejected."
    )


def _load_smart_home_config() -> Dict[str, Any]:
    path = get_configs_dir() / "smart_home_tools.json"
    if not path.exists():
        raise RuntimeError(f"Missing smart-home config file: {path}")
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed reading smart-home bridge config: %s", e)
        logger.debug("smart-home bridge config read exception details", exc_info=True)
        raise
    if not isinstance(payload, dict):
        raise ValueError("configs/smart_home_tools.json must be a JSON object.")
    return payload


def _integration_cfg(integration: str) -> Dict[str, Any]:
    cfg = _load_smart_home_config()
    payload = cfg.get(integration)
    if not isinstance(payload, dict):
        raise ValueError(f"Missing integration config '{integration}'.")
    return payload


def _require_bridge_auth(integration: str) -> None:
    cfg = _integration_cfg(integration)
    token_env_var = str(cfg.get("token_env_var") or "").strip()
    if not token_env_var:
        raise RuntimeError(f"Integration '{integration}' missing token_env_var in config.")
    expected = str(os.environ.get(token_env_var) or "").strip()
    if not expected:
        raise RuntimeError(
            f"Integration '{integration}' auth env var '{token_env_var}' is not set in runtime."
        )

    auth_header = str(request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise PermissionError("Missing bearer Authorization header.")
    supplied = auth_header[7:].strip()
    if not supplied:
        raise PermissionError("Empty bearer token.")
    if not hmac.compare_digest(supplied, expected):
        raise PermissionError("Invalid bearer token.")


def _require_env(key: str) -> str:
    value = str(os.environ.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Required env var '{key}' is not set.")
    return value


def _nest_project_id() -> str:
    return _require_env("NEST_PROJECT_ID")


def _sdm_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _sdm_devices_list(access_token: str) -> List[Dict[str, Any]]:
    project_id = _nest_project_id()
    url = f"{_SDM_BASE_URL}/enterprises/{project_id}/devices"
    try:
        response = requests.get(url, headers=_sdm_headers(access_token), timeout=20)
    except Exception as e:
        logger.error("Nest bridge failed calling devices.list: %s", e)
        logger.debug("Nest devices.list request exception details", exc_info=True)
        raise
    if response.status_code >= 400:
        preview = (response.text or "")[:600]
        raise RuntimeError(
            f"Nest devices.list failed HTTP {response.status_code}. Response: {preview}"
        )
    try:
        data = response.json()
    except Exception as e:
        logger.error("Nest bridge failed parsing devices.list response: %s", e)
        logger.debug("Nest devices.list parse exception details", exc_info=True)
        raise
    if not isinstance(data, dict):
        raise ValueError("Nest devices.list response must be a JSON object.")
    devices = data.get("devices")
    if devices is None:
        return []
    if not isinstance(devices, list):
        raise ValueError("Nest devices.list response field 'devices' must be a list.")
    return [d for d in devices if isinstance(d, dict)]


def _resolve_thermostat_device_name(devices: List[Dict[str, Any]], device_id: str) -> str:
    thermostats = []
    for device in devices:
        dtype = str(device.get("type") or "").strip()
        if dtype.endswith("THERMOSTAT"):
            thermostats.append(device)
    if not thermostats:
        raise RuntimeError("No thermostat devices found in Nest account.")

    requested = str(device_id or "").strip()
    if not requested:
        if len(thermostats) == 1:
            name = str(thermostats[0].get("name") or "").strip()
            if not name:
                raise RuntimeError("Thermostat device missing name field.")
            return name
        raise RuntimeError(
            "Multiple thermostats found. Provide arguments.device_id to select one."
        )

    for device in thermostats:
        name = str(device.get("name") or "").strip()
        if not name:
            continue
        if requested == name or name.endswith(f"/{requested}"):
            return name
    raise RuntimeError(f"No thermostat matches device_id '{requested}'.")


def _sdm_execute_command(
    *,
    access_token: str,
    device_name: str,
    command: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    if not device_name:
        raise ValueError("device_name is required.")
    url = f"{_SDM_BASE_URL}/{device_name}:executeCommand"
    payload = {"command": command, "params": params if isinstance(params, dict) else {}}
    try:
        response = requests.post(url, headers=_sdm_headers(access_token), json=payload, timeout=20)
    except Exception as e:
        logger.error("Nest bridge executeCommand request failed: %s", e)
        logger.debug("Nest executeCommand request exception details", exc_info=True)
        raise
    if response.status_code >= 400:
        preview = (response.text or "")[:600]
        raise RuntimeError(
            f"Nest executeCommand failed HTTP {response.status_code}. Response: {preview}"
        )
    try:
        data = response.json()
    except Exception as e:
        logger.error("Nest bridge failed parsing executeCommand response: %s", e)
        logger.debug("Nest executeCommand parse exception details", exc_info=True)
        raise
    if not isinstance(data, dict):
        raise ValueError("Nest executeCommand response must be a JSON object.")
    return data


def _summarize_thermostat(device: Dict[str, Any]) -> Dict[str, Any]:
    traits = device.get("traits") if isinstance(device.get("traits"), dict) else {}
    mode_trait = traits.get("sdm.devices.traits.ThermostatMode")
    temp_trait = traits.get("sdm.devices.traits.Temperature")
    humidity_trait = traits.get("sdm.devices.traits.Humidity")
    eco_trait = traits.get("sdm.devices.traits.ThermostatEco")
    setpoint_trait = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint")
    return {
        "name": str(device.get("name") or ""),
        "type": str(device.get("type") or ""),
        "mode": str(mode_trait.get("mode") if isinstance(mode_trait, dict) else ""),
        "ambient_temperature_c": (
            temp_trait.get("ambientTemperatureCelsius")
            if isinstance(temp_trait, dict)
            else None
        ),
        "humidity_percent": (
            humidity_trait.get("ambientHumidityPercent")
            if isinstance(humidity_trait, dict)
            else None
        ),
        "target_heat_c": (
            setpoint_trait.get("heatCelsius")
            if isinstance(setpoint_trait, dict)
            else None
        ),
        "target_cool_c": (
            setpoint_trait.get("coolCelsius")
            if isinstance(setpoint_trait, dict)
            else None
        ),
        "eco_mode": str(eco_trait.get("mode") if isinstance(eco_trait, dict) else ""),
    }


def _nest_get_status(access_token: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    devices = _sdm_devices_list(access_token)
    device_id = str(arguments.get("device_id") or "").strip()
    if device_id:
        selected_name = _resolve_thermostat_device_name(devices, device_id)
        selected = next((d for d in devices if str(d.get("name") or "").strip() == selected_name), None)
        if not isinstance(selected, dict):
            raise RuntimeError(f"Could not load thermostat details for '{device_id}'.")
        return {"thermostat": _summarize_thermostat(selected)}
    thermostats = [_summarize_thermostat(d) for d in devices if str(d.get("type") or "").endswith("THERMOSTAT")]
    return {"thermostats": thermostats}


def _nest_set_mode(access_token: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    # Normalize common variants before comparing: lowercase, underscore-separated,
    # and hyphenated forms (e.g. 'heat_cool', 'heat-cool', 'Heat Cool') all map to
    # the SDM API's canonical 'HEATCOOL'. Without this, LLM-generated arguments
    # that follow the contract's documented lowercase style ('heat_cool') got
    # rejected, which silently broke set_mode calls.
    raw = str(arguments.get("mode") or "").strip()
    mode = raw.upper().replace("_", "").replace("-", "").replace(" ", "")
    allowed = {"HEAT", "COOL", "HEATCOOL", "OFF"}
    if mode not in allowed:
        raise ValueError(
            f"Nest set_mode requires mode in: HEAT, COOL, HEATCOOL, OFF. Got {raw!r}."
        )
    devices = _sdm_devices_list(access_token)
    device_name = _resolve_thermostat_device_name(devices, str(arguments.get("device_id") or ""))
    result = _sdm_execute_command(
        access_token=access_token,
        device_name=device_name,
        command="sdm.devices.commands.ThermostatMode.SetMode",
        params={"mode": mode},
    )
    return {"device_name": device_name, "command_result": result}


def _nest_set_target_temperature(access_token: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    target_c, provided_unit = _resolve_nest_target_temperature_c(arguments)
    _validate_nest_target_temperature_c(target_c)
    target_c = round(target_c, 2)
    devices = _sdm_devices_list(access_token)
    device_name = _resolve_thermostat_device_name(devices, str(arguments.get("device_id") or ""))
    selected = next((d for d in devices if str(d.get("name") or "").strip() == device_name), None)
    if not isinstance(selected, dict):
        raise RuntimeError(f"Could not load thermostat details for '{device_name}'.")
    traits = selected.get("traits") if isinstance(selected.get("traits"), dict) else {}
    mode_trait = traits.get("sdm.devices.traits.ThermostatMode")
    current_mode = str(mode_trait.get("mode") if isinstance(mode_trait, dict) else "").strip().upper()

    command_mode = str(arguments.get("mode") or "").strip().upper()
    if command_mode:
        if command_mode not in {"HEAT", "COOL"}:
            raise ValueError("When provided, mode must be HEAT or COOL for set_target_temperature.")
    else:
        command_mode = current_mode

    if command_mode == "HEAT":
        command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
        params = {"heatCelsius": target_c}
    elif command_mode == "COOL":
        command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
        params = {"coolCelsius": target_c}
    else:
        raise RuntimeError(
            "Thermostat mode is not HEAT/COOL. Provide arguments.mode as HEAT or COOL."
        )

    result = _sdm_execute_command(
        access_token=access_token,
        device_name=device_name,
        command=command,
        params=params,
    )
    return {
        "device_name": device_name,
        "command_result": result,
        "applied_target_temperature_c": target_c,
        "input_unit": provided_unit,
    }


def _nest_set_eco_mode(access_token: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    enabled = arguments.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("Nest set_eco_mode requires boolean enabled.")
    devices = _sdm_devices_list(access_token)
    device_name = _resolve_thermostat_device_name(devices, str(arguments.get("device_id") or ""))
    mode = "MANUAL_ECO" if enabled else "OFF"
    result = _sdm_execute_command(
        access_token=access_token,
        device_name=device_name,
        command="sdm.devices.commands.ThermostatEco.SetMode",
        params={"mode": mode},
    )
    return {"device_name": device_name, "command_result": result}


def _nest_set_fan_mode(access_token: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    fan_mode = str(arguments.get("fan_mode") or "").strip().lower()
    if fan_mode not in {"on", "auto", "off"}:
        raise ValueError("Nest set_fan_mode requires fan_mode in: on, auto, off.")
    if fan_mode in {"auto", "off"}:
        raise RuntimeError(
            "Nest fan auto/off is not supported through this bridge yet. Use fan_mode='on'."
        )
    devices = _sdm_devices_list(access_token)
    device_name = _resolve_thermostat_device_name(devices, str(arguments.get("device_id") or ""))
    # 15-minute timer as a safe explicit fan activation.
    result = _sdm_execute_command(
        access_token=access_token,
        device_name=device_name,
        command="sdm.devices.commands.Fan.SetTimer",
        params={"timerMode": "ON", "duration": "900s"},
    )
    return {"device_name": device_name, "command_result": result}


def _nest_dispatch(action: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    creds = load_google_credentials(
        account_id=NEST_GOOGLE_ACCOUNT_ID,
        required_scopes=_NEST_REQUIRED_SCOPES,
    )
    access_token = creds.token
    action_norm = str(action or "").strip().lower()
    if action_norm == "get_status":
        return _nest_get_status(access_token, arguments)
    if action_norm == "set_mode":
        return _nest_set_mode(access_token, arguments)
    if action_norm == "set_target_temperature":
        return _nest_set_target_temperature(access_token, arguments)
    if action_norm == "set_eco_mode":
        return _nest_set_eco_mode(access_token, arguments)
    if action_norm == "set_fan_mode":
        return _nest_set_fan_mode(access_token, arguments)
    raise ValueError(f"Unsupported nest action '{action_norm}'.")


def _extract_payload() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field_name} must be an integer.")
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field_name} must be an integer.")
        if raw.startswith("-"):
            digits = raw[1:]
        else:
            digits = raw
        if not digits.isdigit():
            raise ValueError(f"{field_name} must be an integer.")
        return int(raw)
    raise ValueError(f"{field_name} must be an integer.")


def _lights_cfg() -> Dict[str, Any]:
    cfg = _integration_cfg("lights")
    hosts_raw = cfg.get("kasa_device_hosts", [])
    if hosts_raw is None:
        hosts_raw = []
    if not isinstance(hosts_raw, list):
        raise ValueError("lights.kasa_device_hosts must be a list of host strings.")
    hosts = [str(x).strip() for x in hosts_raw if isinstance(x, str) and str(x).strip()]
    timeout_raw = cfg.get("kasa_discovery_timeout_seconds", 4)
    timeout_seconds = _coerce_int(timeout_raw, "kasa_discovery_timeout_seconds")
    if timeout_seconds <= 0:
        raise ValueError("kasa_discovery_timeout_seconds must be > 0.")
    return {"hosts": hosts, "timeout_seconds": timeout_seconds}


def _lights_hosts_override(arguments: Dict[str, Any]) -> List[str]:
    hosts_raw = arguments.get("kasa_device_hosts")
    if hosts_raw is None:
        return []
    if not isinstance(hosts_raw, list):
        raise ValueError("arguments.kasa_device_hosts must be a list of host strings.")
    hosts = [str(x).strip() for x in hosts_raw if isinstance(x, str) and str(x).strip()]
    if not hosts:
        raise ValueError(
            "arguments.kasa_device_hosts was provided but no valid host strings were found."
        )
    return hosts


def _kasa_identity(device: Any) -> Dict[str, Any]:
    alias = str(getattr(device, "alias", "") or "").strip()
    host = str(getattr(device, "host", "") or "").strip()
    model = str(getattr(device, "model", "") or "").strip()
    dev_type = str(getattr(device, "device_type", "") or "").strip()
    brightness = getattr(device, "brightness", None)
    is_on = bool(getattr(device, "is_on", False))
    return {
        "light_id": host or alias or model,
        "alias": alias,
        "host": host,
        "model": model,
        "device_type": dev_type,
        "is_on": is_on,
        "brightness_pct": brightness if isinstance(brightness, int) else None,
    }


def _kasa_target_match(device: Any, *, light_id: str, room: str) -> bool:
    identity = _kasa_identity(device)
    if light_id:
        lid = light_id.strip().lower()
        candidates = {
            str(identity.get("light_id") or "").strip().lower(),
            str(identity.get("alias") or "").strip().lower(),
            str(identity.get("host") or "").strip().lower(),
        }
        if lid not in candidates:
            return False
    if room:
        room_norm = room.strip().lower()
        alias_norm = str(identity.get("alias") or "").strip().lower()
        if room_norm not in alias_norm:
            return False
    return True


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except Exception as e:
        logger.error("Kasa async bridge execution failed: %s", e)
        logger.debug("Kasa async bridge execution exception details", exc_info=True)
        raise


async def _kasa_load_devices(hosts: List[str], timeout_seconds: int) -> List[Any]:
    try:
        from kasa import Discover
    except Exception as e:
        logger.error("python-kasa import failed: %s", e)
        logger.debug("python-kasa import exception details", exc_info=True)
        raise RuntimeError(
            "python-kasa dependency is missing. Install 'python-kasa' to use lights integration."
        )

    devices: List[Any] = []
    if hosts:
        for host in hosts:
            try:
                device = await Discover.discover_single(host, timeout=timeout_seconds)
            except Exception as e:
                logger.error("Kasa discover_single failed for host '%s': %s", host, e)
                logger.debug("Kasa discover_single exception details", exc_info=True)
                raise
            if device is None:
                raise RuntimeError(f"Kasa device not found at host '{host}'.")
            await device.update()
            devices.append(device)
    else:
        try:
            discovered = await Discover.discover(timeout=timeout_seconds)
        except Exception as e:
            logger.error("Kasa network discovery failed: %s", e)
            logger.debug("Kasa network discovery exception details", exc_info=True)
            raise
        if not isinstance(discovered, dict):
            raise RuntimeError("Kasa discovery returned invalid payload.")
        for _host, device in discovered.items():
            if device is None:
                continue
            await device.update()
            devices.append(device)

    if not devices:
        raise RuntimeError(
            "No Kasa devices found. Configure kasa_device_hosts in smart_home_tools.json "
            "or ensure devices are reachable on the local network."
        )
    return devices


async def _kasa_list_lights(*, hosts: List[str], timeout_seconds: int) -> Dict[str, Any]:
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    lights = [_kasa_identity(device) for device in devices]
    return {"lights": lights}


async def _kasa_discover_hosts(*, timeout_seconds: int) -> Dict[str, Any]:
    devices = await _kasa_load_devices([], timeout_seconds)
    lights = [_kasa_identity(device) for device in devices]
    hosts = [str(light.get("host") or "").strip() for light in lights if str(light.get("host") or "").strip()]
    return {"hosts": hosts, "lights": lights}


async def _kasa_set_light_power(
    *,
    hosts: List[str],
    timeout_seconds: int,
    state: str,
    light_id: str,
    room: str,
) -> Dict[str, Any]:
    state_norm = str(state or "").strip().lower()
    if state_norm not in {"on", "off"}:
        raise ValueError("set_light_power requires state in: on, off.")
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    selected = [d for d in devices if _kasa_target_match(d, light_id=light_id, room=room)]
    if not selected:
        raise RuntimeError("No lights matched the requested selectors (light_id/room).")

    changed = []
    for device in selected:
        if state_norm == "on":
            await device.turn_on()
        else:
            await device.turn_off()
        await device.update()
        changed.append(_kasa_identity(device))
    return {"changed": changed, "state": state_norm}


async def _kasa_set_light_brightness(
    *,
    hosts: List[str],
    timeout_seconds: int,
    brightness_pct: int,
    light_id: str,
    room: str,
) -> Dict[str, Any]:
    if brightness_pct < 0 or brightness_pct > 100:
        raise ValueError("brightness_pct must be between 0 and 100.")
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    selected = [d for d in devices if _kasa_target_match(d, light_id=light_id, room=room)]
    if not selected:
        raise RuntimeError("No lights matched the requested selectors (light_id/room).")

    changed = []
    for device in selected:
        if not hasattr(device, "set_brightness"):
            identity = _kasa_identity(device)
            raise RuntimeError(
                f"Device '{identity.get('alias') or identity.get('host')}' does not support brightness control."
            )
        await device.set_brightness(brightness_pct)
        await device.update()
        changed.append(_kasa_identity(device))
    return {"changed": changed, "brightness_pct": brightness_pct}


def _lights_dispatch(action: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    action_norm = str(action or "").strip().lower()
    cfg = _lights_cfg()
    hosts_override = _lights_hosts_override(arguments)
    hosts = hosts_override if hosts_override else cfg["hosts"]
    timeout_seconds = cfg["timeout_seconds"]
    light_id = str(arguments.get("light_id") or "").strip()
    room = str(arguments.get("room") or "").strip()

    if action_norm == "list_lights":
        return _run_async(_kasa_list_lights(hosts=hosts, timeout_seconds=timeout_seconds))
    if action_norm == "discover_hosts":
        return _run_async(_kasa_discover_hosts(timeout_seconds=timeout_seconds))
    if action_norm == "set_light_power":
        state = str(arguments.get("state") or "").strip().lower()
        return _run_async(
            _kasa_set_light_power(
                hosts=hosts,
                timeout_seconds=timeout_seconds,
                state=state,
                light_id=light_id,
                room=room,
            )
        )
    if action_norm == "set_light_brightness":
        brightness_pct = _coerce_int(arguments.get("brightness_pct"), "brightness_pct")
        return _run_async(
            _kasa_set_light_brightness(
                hosts=hosts,
                timeout_seconds=timeout_seconds,
                brightness_pct=brightness_pct,
                light_id=light_id,
                room=room,
            )
        )
    if action_norm == "set_light_color":
        raise RuntimeError(
            "set_light_color is not supported for Kasa HS220 dimmer switches."
        )
    if action_norm == "set_scene":
        raise RuntimeError(
            "set_scene is not implemented for Kasa local control yet."
        )
    raise ValueError(f"Unsupported lights action '{action_norm}'.")


@smart_home_bridge_bp.route("/api/smart-home/nest", methods=["POST"])
def smart_home_nest():
    try:
        _require_bridge_auth("nest")
        payload = _extract_payload()
        action = str(payload.get("action") or "").strip()
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")
        result = _nest_dispatch(action, arguments)
        return jsonify({"ok": True, "integration": "nest", "action": action, "result": result})
    except Exception as e:
        logger.error("smart_home_nest failed: %s", e)
        logger.debug("smart_home_nest exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400


@smart_home_bridge_bp.route("/api/smart-home/lights", methods=["POST"])
def smart_home_lights():
    try:
        _require_bridge_auth("lights")
        payload = _extract_payload()
        action = str(payload.get("action") or "").strip()
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")
        result = _lights_dispatch(action, arguments)
        return jsonify({"ok": True, "integration": "lights", "action": action, "result": result})
    except Exception as e:
        logger.error("smart_home_lights failed: %s", e)
        logger.debug("smart_home_lights exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400


@smart_home_bridge_bp.route("/api/smart-home/ring", methods=["POST"])
def smart_home_ring():
    try:
        _require_bridge_auth("ring")
        raise RuntimeError("Ring bridge is not implemented yet inside Emi.")
    except Exception as e:
        logger.error("smart_home_ring failed: %s", e)
        logger.debug("smart_home_ring exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400
