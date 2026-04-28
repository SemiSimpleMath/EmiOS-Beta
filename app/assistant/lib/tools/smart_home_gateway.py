from __future__ import annotations

import json
import os
from typing import Any, Dict

import requests

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir

logger = get_logger(__name__)


def _integration_label(integration: str) -> str:
    return str(integration or "").strip()


def _load_smart_home_config() -> Dict[str, Any]:
    config_path = get_configs_dir() / "smart_home_tools.json"
    if not config_path.exists():
        raise RuntimeError(f"Missing required smart-home config: {config_path}")
    try:
        raw = config_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except Exception as e:
        logger.error("Failed reading smart-home config file: %s", e)
        logger.debug("smart-home config read exception details", exc_info=True)
        raise
    if not isinstance(payload, dict):
        raise ValueError("configs/smart_home_tools.json must be a JSON object.")
    return payload


def _resolve_integration_config(integration: str) -> Dict[str, Any]:
    payload = _load_smart_home_config()
    integration_cfg = payload.get(integration)
    integration_label = _integration_label(integration)
    if not isinstance(integration_cfg, dict):
        raise ValueError(f"Missing integration config '{integration_label}' in smart_home_tools.json.")

    if not bool(integration_cfg.get("enabled", False)):
        raise RuntimeError(
            f"Smart-home integration '{integration_label}' is disabled. "
            "Set enabled=true in configs/smart_home_tools.json."
        )

    endpoint_url = str(integration_cfg.get("endpoint_url") or "").strip()
    if not endpoint_url:
        raise ValueError(
            f"Integration '{integration_label}' requires non-empty endpoint_url in configs/smart_home_tools.json."
        )

    token_env_var = str(integration_cfg.get("token_env_var") or "").strip()
    if not token_env_var:
        raise ValueError(
            f"Integration '{integration_label}' requires non-empty token_env_var in configs/smart_home_tools.json."
        )
    token = str(os.environ.get(token_env_var) or "").strip()
    if not token:
        raise RuntimeError(
            f"Integration '{integration_label}' requires environment variable '{token_env_var}' to be set."
        )

    timeout_seconds_raw = integration_cfg.get("timeout_seconds", 20)
    try:
        timeout_seconds = int(timeout_seconds_raw)
    except Exception as e:
        logger.error(
            "Invalid timeout_seconds for integration '%s': %r (%s)",
            integration,
            timeout_seconds_raw,
            e,
        )
        logger.debug("smart-home timeout parse exception details", exc_info=True)
        raise
    if timeout_seconds <= 0:
        raise ValueError(f"Integration '{integration_label}' timeout_seconds must be > 0.")

    return {
        "endpoint_url": endpoint_url,
        "token": token,
        "timeout_seconds": timeout_seconds,
    }


def send_smart_home_command(
    *,
    integration: str,
    action: str,
    arguments: Dict[str, Any],
    request_id: str | None = None,
) -> Dict[str, Any]:
    cfg = _resolve_integration_config(integration)
    endpoint_url = cfg["endpoint_url"]
    timeout_seconds = cfg["timeout_seconds"]
    token = cfg["token"]

    payload = {
        "integration": integration,
        "action": str(action or "").strip(),
        "arguments": arguments if isinstance(arguments, dict) else {},
        "request_id": str(request_id or "").strip(),
    }
    if not payload["action"]:
        raise ValueError("action must be a non-empty string.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            endpoint_url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
    except Exception as e:
        logger.error(
            "Smart-home request failed (integration=%s action=%s endpoint=%s): %s",
            integration,
            action,
            endpoint_url,
            e,
        )
        logger.debug("smart-home request exception details", exc_info=True)
        raise

    if response.status_code >= 400:
        body_preview = (response.text or "")[:600]
        raise RuntimeError(
            "Smart-home endpoint returned HTTP "
            f"{response.status_code} for integration='{integration}' action='{action}'. "
            f"Response: {body_preview}"
        )

    try:
        data = response.json()
    except Exception as e:
        logger.error(
            "Smart-home endpoint returned non-JSON response (integration=%s action=%s): %s",
            integration,
            action,
            e,
        )
        logger.debug("smart-home response parse exception details", exc_info=True)
        raise

    if not isinstance(data, dict):
        raise ValueError(
            "Smart-home endpoint must return a JSON object. "
            f"Got {type(data).__name__}."
        )
    if data.get("ok") is False:
        error_msg = str(data.get("error") or "unknown endpoint error").strip() or "unknown endpoint error"
        raise RuntimeError(
            f"Smart-home command failed for integration='{integration}' action='{action}': {error_msg}"
        )
    return data
