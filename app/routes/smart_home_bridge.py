from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any, Dict, List, Optional

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


def _autoset_loopback_secrets() -> None:
    """Auto-set per-integration bearer secrets when missing from env.

    The token_env_var on each smart-home integration is a SHARED SECRET
    used between this Flask process's UI/gateway and its own bridge
    endpoint (pure loopback). It is NOT the third-party API key — those
    live elsewhere (Ring: data/ring_token.json; Nest: Google OAuth;
    Kasa: local LAN, no token). Auto-generating a random value when the
    env var is absent removes a setup footgun without changing security:
    if the user later exports a real value, that takes precedence and we
    don't overwrite it.
    """
    import secrets as _secrets
    try:
        payload = _load_smart_home_config()
    except Exception:
        return  # don't crash module import if config is missing
    for integration_key, integration_cfg in payload.items():
        if not isinstance(integration_cfg, dict):
            continue
        env_var = str(integration_cfg.get("token_env_var") or "").strip()
        if not env_var:
            continue
        if str(os.environ.get(env_var) or "").strip():
            continue  # user already set this — respect it
        os.environ[env_var] = _secrets.token_urlsafe(32)


_autoset_loopback_secrets()


def _resolve_smart_home_alias(integration: str, value: Any, id_field: str) -> Any:
    """Resolve a configured device alias to its raw identifier.

    `value` is whatever the planner passed (alias or already-raw id).
    Looks up `configs/smart_home_tools.json[integration].devices[*]` for a
    record whose ``alias`` matches ``value``; if found, returns
    ``record[id_field]`` (e.g. ``host`` for lights, ``camera_id`` for ring,
    ``device_id`` for nest).

    If no alias matches OR no devices are configured, returns ``value``
    unchanged — preserves "raw id passed by planner" backward-compat.
    Case-insensitive on alias.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    target = value.strip().lower()
    try:
        cfg = _integration_cfg(integration)
        devices = cfg.get("devices") if isinstance(cfg.get("devices"), list) else []
    except Exception:
        return value
    for d in devices:
        if not isinstance(d, dict):
            continue
        alias = str(d.get("alias") or "").strip()
        if not alias:
            continue
        if alias.lower() == target:
            resolved = d.get(id_field)
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
    return value


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
    # device_id didn't match an actual thermostat. If there's exactly
    # one thermostat, fall back to it — the planner often passes the
    # entity-card label ("Nest thermostat") as device_id, which won't
    # match Google's internal device name. Logging the substitution
    # so the failure is visible in the audit trail without bouncing
    # the user-visible call.
    if len(thermostats) == 1:
        name = str(thermostats[0].get("name") or "").strip()
        if name:
            logger.warning(
                "[smart_home/nest] device_id %r did not match; falling back to "
                "the only thermostat %r",
                requested, name,
            )
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
    # Normalize common variants before comparing: lowercase / underscore /
    # hyphen / space-separated forms all map to the SDM canonical form.
    # HEATCOOL was removed (Jukka's preference — only single-mode HEAT or COOL).
    raw = str(arguments.get("mode") or "").strip()
    mode = raw.upper().replace("_", "").replace("-", "").replace(" ", "")
    allowed = {"HEAT", "COOL", "OFF"}
    if mode not in allowed:
        raise ValueError(
            f"Nest set_mode requires mode in: HEAT, COOL, OFF. Got {raw!r}."
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


def _nest_dispatch(command: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    creds = load_google_credentials(
        account_id=NEST_GOOGLE_ACCOUNT_ID,
        required_scopes=_NEST_REQUIRED_SCOPES,
    )
    access_token = creds.token
    command_norm = str(command or "").strip().lower()

    # Alias resolution: planner may pass device_id as a configured alias
    # ("Main Floor") instead of the raw Nest device id. Resolve once up front.
    if isinstance(arguments.get("device_id"), str) and arguments["device_id"].strip():
        resolved = _resolve_smart_home_alias("nest", arguments["device_id"], "device_id")
        if resolved != arguments["device_id"]:
            arguments = dict(arguments)
            arguments["device_id"] = resolved

    if command_norm == "get_status":
        return _nest_get_status(access_token, arguments)
    if command_norm == "set_mode":
        return _nest_set_mode(access_token, arguments)
    if command_norm == "set_target_temperature":
        return _nest_set_target_temperature(access_token, arguments)
    if command_norm == "set_eco_mode":
        return _nest_set_eco_mode(access_token, arguments)
    if command_norm == "set_fan_mode":
        return _nest_set_fan_mode(access_token, arguments)
    raise ValueError(f"Unsupported nest command '{command_norm}'.")


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
    """Read configured Kasa lights. Two source fields exist:

      - lights.devices[]  — canonical, written by the lights settings UI.
                            Each entry is {alias, host, notes}.
      - lights.kasa_device_hosts — legacy flat host list. Kept for
                            back-compat; new UI never writes here.

    Hosts are unioned and deduped. The user-configured alias on
    devices[] is propagated through `host_alias_map` so identity /
    target-match prefer it over the device's hardware-reported alias.
    """
    cfg = _integration_cfg("lights")

    # Canonical: lights.devices[].host (object array, written by UI)
    devices_raw = cfg.get("devices") or []
    if not isinstance(devices_raw, list):
        raise ValueError("lights.devices must be a list.")
    host_alias_map: Dict[str, str] = {}
    hosts_from_devices: List[str] = []
    for d in devices_raw:
        if not isinstance(d, dict):
            continue
        host = str(d.get("host") or "").strip()
        if not host:
            continue
        if host not in host_alias_map:
            hosts_from_devices.append(host)
            alias = str(d.get("alias") or "").strip()
            if alias:
                host_alias_map[host] = alias

    # Back-compat: lights.kasa_device_hosts (flat string array)
    legacy_raw = cfg.get("kasa_device_hosts", [])
    if legacy_raw is None:
        legacy_raw = []
    if not isinstance(legacy_raw, list):
        raise ValueError("lights.kasa_device_hosts must be a list of host strings.")
    legacy_hosts = [str(x).strip() for x in legacy_raw if isinstance(x, str) and str(x).strip()]

    # Union, preserving order: devices first (canonical), then legacy-only.
    hosts = list(hosts_from_devices)
    for h in legacy_hosts:
        if h not in hosts:
            hosts.append(h)

    timeout_raw = cfg.get("kasa_discovery_timeout_seconds", 4)
    timeout_seconds = _coerce_int(timeout_raw, "kasa_discovery_timeout_seconds")
    if timeout_seconds <= 0:
        raise ValueError("kasa_discovery_timeout_seconds must be > 0.")
    return {
        "hosts": hosts,
        "timeout_seconds": timeout_seconds,
        "host_alias_map": host_alias_map,
    }


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


def _kasa_identity(device: Any, host_alias_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Build a result-dict for one Kasa device. When `host_alias_map`
    contains an entry for this device's host, the user-configured alias
    from `lights.devices[].alias` overrides the device's hardware-
    reported alias (so when the planner says 'Living room light', it
    matches what the user labeled in the UI even if the bulb's
    internal Kasa-app alias is different)."""
    hw_alias = str(getattr(device, "alias", "") or "").strip()
    host = str(getattr(device, "host", "") or "").strip()
    model = str(getattr(device, "model", "") or "").strip()
    dev_type = str(getattr(device, "device_type", "") or "").strip()
    brightness = getattr(device, "brightness", None)
    is_on = bool(getattr(device, "is_on", False))

    user_alias = ""
    if host_alias_map and host:
        user_alias = (host_alias_map.get(host) or "").strip()
    effective_alias = user_alias or hw_alias

    return {
        "light_id": host or effective_alias or model,
        "alias": effective_alias,
        "hw_alias": hw_alias,
        "user_alias": user_alias,
        "host": host,
        "model": model,
        "device_type": dev_type,
        "is_on": is_on,
        "brightness_pct": brightness if isinstance(brightness, int) else None,
    }


def _kasa_target_match(
    device: Any,
    *,
    light_id: str,
    room: str,
    host_alias_map: Optional[Dict[str, str]] = None,
) -> bool:
    identity = _kasa_identity(device, host_alias_map=host_alias_map)
    if light_id:
        lid = light_id.strip().lower()
        # Match against any of: composite light_id, user-alias,
        # hw-alias, or host. User-alias is the one the planner usually
        # produces from the lights tool description, so it's the
        # primary success path.
        candidates = {
            str(identity.get("light_id") or "").strip().lower(),
            str(identity.get("alias") or "").strip().lower(),
            str(identity.get("user_alias") or "").strip().lower(),
            str(identity.get("hw_alias") or "").strip().lower(),
            str(identity.get("host") or "").strip().lower(),
        }
        if lid not in candidates:
            return False
    if room:
        room_norm = room.strip().lower()
        # Look at BOTH user and hw alias for room-substring matching.
        alias_haystack = " ".join([
            str(identity.get("user_alias") or ""),
            str(identity.get("hw_alias") or ""),
        ]).lower()
        if room_norm not in alias_haystack:
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
    """Load Kasa devices from an EXPLICIT host list. Never scans the LAN.

    Architectural rule: Kasa lights are only ever the devices the user
    explicitly configured AS Kasa lights (via the lights settings UI).
    Runtime operations (list_lights, set_light_power, etc.) MUST NOT
    fall back to LAN-wide discovery — that path picks up other TP-Link
    devices on the network (Tapo cameras, plugs, etc.) and tries to
    speak the Kasa protocol to them, which fails noisily and corrupts
    the result. LAN scanning is a one-shot UI helper exposed via
    `_kasa_scan_lan` for the settings page; it does NOT participate
    in the runtime data path.

    Empty hosts → returns []. The caller decides whether that's an
    error (e.g. set_light_power with nothing configured) or a clean
    "no lights" response (list_lights with nothing configured).
    """
    if not hosts:
        return []

    try:
        from kasa import Discover
    except Exception as e:
        logger.error("python-kasa import failed: %s", e)
        logger.debug("python-kasa import exception details", exc_info=True)
        raise RuntimeError(
            "python-kasa dependency is missing. Install 'python-kasa' to use lights integration."
        )

    devices: List[Any] = []
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
    return devices


async def _kasa_scan_lan(timeout_seconds: int) -> List[Any]:
    """One-shot LAN-wide Kasa discovery. Called ONLY from the settings
    UI's discover endpoint, never from runtime data ops.

    Skips per-device any host that responds to the Kasa discovery probe
    but fails the protocol handshake (most commonly Tapo cameras —
    TP-Link makes both lines, the discovery probes overlap, but the
    auth/protocol layer diverges → SSL handshake failure).
    """
    try:
        from kasa import Discover
    except Exception as e:
        logger.error("python-kasa import failed: %s", e)
        raise RuntimeError(
            "python-kasa dependency is missing. Install 'python-kasa' to use lights integration."
        )

    try:
        discovered = await Discover.discover(timeout=timeout_seconds)
    except Exception as e:
        logger.error("Kasa network discovery failed: %s", e)
        logger.debug("Kasa network discovery exception details", exc_info=True)
        raise
    if not isinstance(discovered, dict):
        raise RuntimeError("Kasa discovery returned invalid payload.")

    devices: List[Any] = []
    for host, device in discovered.items():
        if device is None:
            continue
        try:
            await device.update()
        except Exception as e:
            logger.info(
                "Kasa LAN scan: skipping non-Kasa responder at %s — %s",
                host, e,
            )
            continue
        devices.append(device)
    return devices


async def _kasa_list_lights(
    *,
    hosts: List[str],
    timeout_seconds: int,
    host_alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """List configured Kasa lights. Returns empty list when nothing's
    configured — that's a clean 'no lights' state, not an error."""
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    lights = [_kasa_identity(device, host_alias_map=host_alias_map) for device in devices]
    return {"lights": lights}


async def _kasa_discover_hosts(*, timeout_seconds: int) -> Dict[str, Any]:
    """Settings-UI-only LAN scan. Returns Kasa devices found on the
    local network so the UI can offer them as additions to the
    explicit hosts list. Skips non-Kasa neighbors (Tapo cameras, etc.)
    that respond to the discovery probe but fail the protocol
    handshake."""
    devices = await _kasa_scan_lan(timeout_seconds)
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
    host_alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    state_norm = str(state or "").strip().lower()
    if state_norm not in {"on", "off"}:
        raise ValueError("set_light_power requires state in: on, off.")
    if not hosts:
        raise RuntimeError(
            "No Kasa lights are configured. Add light IPs in "
            "Settings → Smart home → Lights before issuing power commands."
        )
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    selected = [
        d for d in devices
        if _kasa_target_match(d, light_id=light_id, room=room, host_alias_map=host_alias_map)
    ]
    if not selected:
        raise RuntimeError("No lights matched the requested selectors (light_id/room).")

    changed = []
    for device in selected:
        if state_norm == "on":
            await device.turn_on()
        else:
            await device.turn_off()
        await device.update()
        changed.append(_kasa_identity(device, host_alias_map=host_alias_map))
    return {"changed": changed, "state": state_norm}


async def _kasa_set_light_brightness(
    *,
    hosts: List[str],
    timeout_seconds: int,
    brightness_pct: int,
    light_id: str,
    room: str,
    host_alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if brightness_pct < 0 or brightness_pct > 100:
        raise ValueError("brightness_pct must be between 0 and 100.")
    if not hosts:
        raise RuntimeError(
            "No Kasa lights are configured. Add light IPs in "
            "Settings → Smart home → Lights before issuing brightness commands."
        )
    devices = await _kasa_load_devices(hosts, timeout_seconds)
    selected = [
        d for d in devices
        if _kasa_target_match(d, light_id=light_id, room=room, host_alias_map=host_alias_map)
    ]
    if not selected:
        raise RuntimeError("No lights matched the requested selectors (light_id/room).")

    changed = []
    for device in selected:
        if not hasattr(device, "set_brightness"):
            identity = _kasa_identity(device, host_alias_map=host_alias_map)
            raise RuntimeError(
                f"Device '{identity.get('alias') or identity.get('host')}' does not support brightness control."
            )
        await device.set_brightness(brightness_pct)
        await device.update()
        changed.append(_kasa_identity(device, host_alias_map=host_alias_map))
    return {"changed": changed, "brightness_pct": brightness_pct}


def _lights_dispatch(command: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    command_norm = str(command or "").strip().lower()
    cfg = _lights_cfg()
    hosts_override = _lights_hosts_override(arguments)
    hosts = hosts_override if hosts_override else cfg["hosts"]
    timeout_seconds = cfg["timeout_seconds"]
    host_alias_map = cfg.get("host_alias_map") or {}
    light_id = str(arguments.get("light_id") or "").strip()
    room = str(arguments.get("room") or "").strip()

    # Alias resolution: if `room` matches a configured device alias, narrow
    # hosts to just that alias's host. Falls through to today's substring
    # matching (against discovered Kasa device names) when no alias matches.
    if room and not hosts_override:
        resolved_host = _resolve_smart_home_alias("lights", room, "host")
        if resolved_host and resolved_host != room:
            hosts = [resolved_host]
            # Drop `room` so the downstream substring filter doesn't also
            # try to match it against the device's Kasa-reported alias —
            # we've already pinned to the right host.
            arguments = dict(arguments)
            arguments.pop("room", None)
            room = ""

    if command_norm == "list_lights":
        return _run_async(_kasa_list_lights(
            hosts=hosts, timeout_seconds=timeout_seconds,
            host_alias_map=host_alias_map,
        ))
    if command_norm in ("discover_hosts", "scan_lan"):
        # scan_lan is the canonical name (matches the helper). discover_hosts
        # kept as alias for back-compat with any callers that already use it.
        return _run_async(_kasa_discover_hosts(timeout_seconds=timeout_seconds))
    if command_norm == "set_light_power":
        state = str(arguments.get("state") or "").strip().lower()
        return _run_async(
            _kasa_set_light_power(
                hosts=hosts,
                timeout_seconds=timeout_seconds,
                state=state,
                light_id=light_id,
                room=room,
                host_alias_map=host_alias_map,
            )
        )
    if command_norm == "set_light_brightness":
        brightness_pct = _coerce_int(arguments.get("brightness_pct"), "brightness_pct")
        return _run_async(
            _kasa_set_light_brightness(
                hosts=hosts,
                timeout_seconds=timeout_seconds,
                brightness_pct=brightness_pct,
                light_id=light_id,
                room=room,
                host_alias_map=host_alias_map,
            )
        )
    if command_norm == "set_light_color":
        raise RuntimeError(
            "set_light_color is not supported for Kasa HS220 dimmer switches."
        )
    if command_norm == "set_scene":
        raise RuntimeError(
            "set_scene is not implemented for Kasa local control yet."
        )
    raise ValueError(f"Unsupported lights command '{command_norm}'.")


# ---------------------------------------------------------------------------
# Ring camera integration
# ---------------------------------------------------------------------------
#
# Auth: Ring uses an OAuth-style flow with 2FA. There is no plain "API key."
# We bootstrap once via scripts/ring_bootstrap.py (interactive: username +
# password + SMS/email OTP) which writes a token JSON to data/ring_token.json.
# Thereafter ring-doorbell refreshes the access token on its own; the
# token_updater callback below persists the rotated token back to the file.

_RING_TOKEN_FILENAME = "ring_token.json"
_RING_SNAPSHOTS_DIRNAME = "ring_snapshots"
_RING_USER_AGENT = "EmiOS/1.0"


def _ring_data_dir() -> Any:
    from pathlib import Path
    # Mirrors path_utils convention: data/ at repo root.
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data"


def _ring_token_path() -> Any:
    return _ring_data_dir() / _RING_TOKEN_FILENAME


def _ring_snapshots_dir() -> Any:
    d = _ring_data_dir() / _RING_SNAPSHOTS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ring_load_token() -> Dict[str, Any]:
    import json as _json
    path = _ring_token_path()
    if not path.exists():
        raise RuntimeError(
            f"Ring token not found at {path}. Run "
            "`.venv/Scripts/python.exe scripts/ring_bootstrap.py` to authenticate."
        )
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Ring token at {path} is not valid JSON: {exc}") from exc


def _ring_save_token(token: Dict[str, Any]) -> None:
    """Persist a rotated token back to disk. Called by ring-doorbell on refresh."""
    import json as _json
    path = _ring_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(token, indent=2, sort_keys=True), encoding="utf-8")


async def _ring_load_ring_and_auth() -> Any:
    """Build a populated Ring instance + return (ring, auth) so the caller can close auth."""
    try:
        from ring_doorbell import Auth, Ring
    except ImportError as exc:
        raise RuntimeError(
            "ring-doorbell package is not installed. Run "
            "`.venv/Scripts/pip install ring-doorbell` (or `pip install -r requirements.txt`)."
        ) from exc

    token = _ring_load_token()
    auth = Auth(_RING_USER_AGENT, token=token, token_updater=_ring_save_token)
    ring = Ring(auth)
    await ring.async_create_session()
    await ring.async_update_devices()
    return ring, auth


def _ring_find_camera(ring: Any, camera_id: str) -> Any:
    target = str(camera_id).strip()
    if not target:
        raise ValueError("camera_id is required.")
    for cam in ring.devices().video_devices:
        if str(cam.id) == target:
            return cam
    raise RuntimeError(f"No Ring camera found with id '{target}'.")


def _ring_camera_summary(cam: Any) -> Dict[str, Any]:
    return {
        "id": str(cam.id),
        "name": str(getattr(cam, "name", "") or ""),
        "kind": str(getattr(cam, "kind", "") or getattr(cam, "family", "") or ""),
        "model": str(getattr(cam, "model", "") or ""),
        "battery_life": getattr(cam, "battery_life", None),
        "has_siren": bool(cam.has_capability("siren")),
        "has_light": bool(cam.has_capability("light")),
    }


async def _ring_list_cameras() -> Dict[str, Any]:
    ring, auth = await _ring_load_ring_and_auth()
    try:
        cams = list(ring.devices().video_devices)
        return {"count": len(cams), "cameras": [_ring_camera_summary(c) for c in cams]}
    finally:
        await auth.async_close()


async def _ring_capture_snapshot(ring: Any, cam_id: str, retries: int = 15, delay_s: float = 2.0) -> bytes:
    """Capture a fresh snapshot, polling until Ring reports a new timestamp.

    Replaces ring-doorbell's ``async_get_snapshot`` because that helper crashes
    with IndexError when the timestamps list is empty (the library expects a
    cached snapshot to exist and doesn't tolerate "not yet"). Battery cameras
    in particular take 5-15s to wake up and produce a frame.
    """
    import asyncio as _asyncio
    import time as _time
    from ring_doorbell import const as _ring_const

    payload = {"doorbot_ids": [int(cam_id)]}
    # Trigger the refresh.
    await ring.async_query(_ring_const.SNAPSHOT_TIMESTAMP_ENDPOINT, method="POST", json=payload)
    request_time = _time.time()
    for _ in range(retries):
        await _asyncio.sleep(delay_s)
        resp = await ring.async_query(_ring_const.SNAPSHOT_TIMESTAMP_ENDPOINT, method="POST", json=payload)
        data = resp.json()
        timestamps = data.get("timestamps") if isinstance(data, dict) else None
        if not timestamps:
            continue
        ts_ms = timestamps[0].get("timestamp")
        if ts_ms is None:
            continue
        if ts_ms / 1000.0 > request_time:
            img_resp = await ring.async_query(_ring_const.SNAPSHOT_ENDPOINT.format(cam_id))
            return img_resp.content
    raise RuntimeError(
        f"Camera {cam_id} did not produce a snapshot within {int(retries * delay_s)}s. "
        "Camera may be offline, in deep sleep, or snapshot capture is disabled. "
        "Trigger a motion event in the Ring app first to wake the camera, then retry."
    )


async def _ring_capture_from_recent_recording(ring: Any, cam_id: str) -> bytes:
    """Pull the most recent motion event's RECORDING and extract the first
    frame. More reliable than the snapshot endpoint for battery cams:
    Ring already has the video (uploaded after motion fired); we just
    download it and grab a frame.

    Bonus: the frame is from when Ring's motion sensor actually triggered,
    not "now" (which is often 30-90s after the subject left frame).

    Requires a Ring Protect subscription so recordings are produced. Raises
    if no recording is available (no recent events, or recording not yet
    uploaded, or no Ring Protect).
    """
    import asyncio as _asyncio
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile
    import os as _os
    import aiohttp as _aiohttp

    if not _shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed to extract a frame from the Ring recording).")

    cam = _ring_find_camera(ring, str(cam_id))
    history = await cam.async_history(limit=1)
    if not history:
        raise RuntimeError("No recent events for camera (history empty).")

    event = history[0]
    event_id = event.get("id") if isinstance(event, dict) else getattr(event, "id", None)
    if not event_id:
        raise RuntimeError("Most recent event has no id.")

    url = await cam.async_recording_url(event_id)
    if not url:
        raise RuntimeError(f"No recording URL for event {event_id} (recording may still be uploading or Ring Protect not active).")

    timeout = _aiohttp.ClientTimeout(total=20)
    async with _aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Ring recording fetch returned HTTP {resp.status}.")
            mp4_bytes = await resp.read()

    if not mp4_bytes:
        raise RuntimeError("Ring recording was empty.")

    with _tempfile.TemporaryDirectory(prefix="ring_recording_") as tmp:
        mp4_path = _os.path.join(tmp, "event.mp4")
        jpg_path = _os.path.join(tmp, "frame.jpg")
        with open(mp4_path, "wb") as f:
            f.write(mp4_bytes)
        # Extract the first frame; -ss 0.5s skips an occasional black opening.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", "0.5",
            "-i", mp4_path,
            "-frames:v", "1", "-q:v", "2",
            jpg_path,
        ]
        try:
            result = await _asyncio.to_thread(
                _subprocess.run, cmd, capture_output=True, timeout=15,
            )
        except _subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg timed out extracting frame from Ring recording.") from exc
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode(errors="replace").strip()[-500:]
            raise RuntimeError(f"ffmpeg failed extracting Ring frame: {stderr}")
        with open(jpg_path, "rb") as f:
            return f.read()


async def _ring_get_snapshot(camera_id: str) -> Dict[str, Any]:
    from datetime import datetime, timezone
    ring, auth = await _ring_load_ring_and_auth()
    try:
        cam = _ring_find_camera(ring, camera_id)  # validates the id exists
        # Prefer the recent-recording path (more reliable; what the user
        # actually expects when Ring's app shows a video). Fall back to the
        # snapshot endpoint when no recording is available — first run on a
        # camera, missing Ring Protect, recording still uploading, etc.
        snapshot_bytes = None
        try:
            snapshot_bytes = await _ring_capture_from_recent_recording(ring, str(cam.id))
            logger.info("[ring] frame from recent recording for camera %s (%d bytes)", camera_id, len(snapshot_bytes or b""))
        except Exception as e:
            logger.info("[ring] recording-frame path unavailable for %s (%s); falling back to snapshot endpoint", camera_id, e)
            snapshot_bytes = await _ring_capture_snapshot(ring, str(cam.id))
        if not isinstance(snapshot_bytes, (bytes, bytearray)) or not snapshot_bytes:
            raise RuntimeError("Ring returned an empty snapshot.")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_id = "".join(c for c in str(camera_id) if c.isalnum() or c in "-_")[:32]
        fname = f"{ts}_{safe_id}.jpg"
        out_path = _ring_snapshots_dir() / fname
        out_path.write_bytes(bytes(snapshot_bytes))
        _publish_ring_snapshot_captured(
            snapshot_path=str(out_path),
            filename=fname,
            camera_id=str(camera_id),
            captured_at_utc=ts,
        )
        return {
            "camera_id": str(camera_id),
            "snapshot_path": str(out_path),
            "size_bytes": len(snapshot_bytes),
            "captured_at": ts,
        }
    finally:
        await auth.async_close()


def _publish_ring_snapshot_captured(
    *, snapshot_path: str, filename: str, camera_id: str, captured_at_utc: str
) -> None:
    """Fire the event the ring_analyzer agent listens to.

    Defensive: import + publish are wrapped so a missing event_hub or agent
    never breaks snapshot capture itself. Caption is best-effort, the
    captured JPEG is the main deliverable.
    """
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message
        msg = Message(
            event_topic="ring_snapshot_captured",
            data={
                "snapshot_path": snapshot_path,
                "filename": filename,
                "camera_id": camera_id,
                "captured_at_utc": captured_at_utc,
            },
        )
        DI.event_hub.publish(msg)
    except Exception as e:
        logger.warning("ring_snapshot_captured event publish failed: %s", e)


async def _ring_get_recent_events(
    camera_id: str, lookback_minutes: int
) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone
    ring, auth = await _ring_load_ring_and_auth()
    try:
        cam = _ring_find_camera(ring, camera_id)
        history = await cam.async_history(limit=50)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(lookback_minutes))
        out: List[Dict[str, Any]] = []
        for ev in history or []:
            # ring-doorbell history items expose .created_at (datetime).
            created = ev.get("created_at") if isinstance(ev, dict) else getattr(ev, "created_at", None)
            if created is None:
                continue
            if hasattr(created, "tzinfo") and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
            out.append(
                {
                    "id": str(ev.get("id") if isinstance(ev, dict) else getattr(ev, "id", "")),
                    "kind": str(ev.get("kind") if isinstance(ev, dict) else getattr(ev, "kind", "")),
                    "created_at": str(created),
                    "answered": bool(
                        ev.get("answered") if isinstance(ev, dict) else getattr(ev, "answered", False)
                    ),
                }
            )
        return {"camera_id": str(camera_id), "lookback_minutes": int(lookback_minutes), "count": len(out), "events": out}
    finally:
        await auth.async_close()


async def _ring_set_siren(camera_id: str, enabled: bool) -> Dict[str, Any]:
    ring, auth = await _ring_load_ring_and_auth()
    try:
        cam = _ring_find_camera(ring, camera_id)
        if not cam.has_capability("siren"):
            raise RuntimeError(f"Camera {camera_id} does not support siren.")
        await cam.async_set_siren(1 if enabled else 0)
        return {"camera_id": str(camera_id), "siren": bool(enabled)}
    finally:
        await auth.async_close()


async def _ring_set_light(camera_id: str, enabled: bool) -> Dict[str, Any]:
    ring, auth = await _ring_load_ring_and_auth()
    try:
        cam = _ring_find_camera(ring, camera_id)
        if not cam.has_capability("light"):
            raise RuntimeError(f"Camera {camera_id} does not support light.")
        await cam.async_set_lights("on" if enabled else "off")
        return {"camera_id": str(camera_id), "light": bool(enabled)}
    finally:
        await auth.async_close()


def _ring_dispatch(command: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    command_norm = str(command or "").strip().lower()
    if command_norm == "list_cameras":
        return _run_async(_ring_list_cameras())

    # Alias resolution: planner may pass camera_id as a configured alias
    # ("Front Door") instead of the raw Ring id. Resolve once up front.
    camera_id_raw = str(arguments.get("camera_id") or "").strip()
    camera_id = _resolve_smart_home_alias("ring", camera_id_raw, "camera_id") if camera_id_raw else camera_id_raw

    if command_norm == "get_snapshot":
        return _run_async(_ring_get_snapshot(camera_id))
    if command_norm == "get_recent_events":
        lookback_raw = arguments.get("lookback_minutes")
        lookback = _coerce_int(lookback_raw, "lookback_minutes") if lookback_raw is not None else 60
        return _run_async(_ring_get_recent_events(camera_id, lookback))
    if command_norm == "set_siren":
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("set_siren requires boolean 'enabled'.")
        return _run_async(_ring_set_siren(camera_id, enabled))
    if command_norm == "set_light":
        enabled = arguments.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("set_light requires boolean 'enabled'.")
        return _run_async(_ring_set_light(camera_id, enabled))
    raise ValueError(f"Unsupported ring command '{command_norm}'.")


@smart_home_bridge_bp.route("/api/smart-home/nest", methods=["POST"])
def smart_home_nest():
    try:
        _require_bridge_auth("nest")
        payload = _extract_payload()
        command = str(payload.get("command") or "").strip()
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")
        result = _nest_dispatch(command, arguments)
        return jsonify({"ok": True, "integration": "nest", "command": command, "result": result})
    except Exception as e:
        logger.error("smart_home_nest failed: %s", e)
        logger.debug("smart_home_nest exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400


@smart_home_bridge_bp.route("/api/smart-home/lights", methods=["POST"])
def smart_home_lights():
    try:
        _require_bridge_auth("lights")
        payload = _extract_payload()
        command = str(payload.get("command") or "").strip()
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")
        result = _lights_dispatch(command, arguments)
        return jsonify({"ok": True, "integration": "lights", "command": command, "result": result})
    except Exception as e:
        logger.error("smart_home_lights failed: %s", e)
        logger.debug("smart_home_lights exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400


@smart_home_bridge_bp.route("/api/smart-home/ring", methods=["POST"])
def smart_home_ring():
    try:
        _require_bridge_auth("ring")
        payload = _extract_payload()
        command = str(payload.get("command") or "").strip()
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")
        result = _ring_dispatch(command, arguments)
        return jsonify({"ok": True, "integration": "ring", "command": command, "result": result})
    except Exception as e:
        logger.error("smart_home_ring failed: %s", e)
        logger.debug("smart_home_ring exception details", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 400
