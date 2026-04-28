from __future__ import annotations

import json
import os

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir

logger = get_logger(__name__)


def get_required_assistant_name() -> str:
    env_val = str(os.environ.get("ASSISTANT_NAME") or "").strip()
    if env_val:
        return env_val

    resource_path = get_resources_dir() / "assistant" / "resource_assistant_data.json"
    try:
        if resource_path.exists():
            payload = json.loads(resource_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                name = str(payload.get("name") or "").strip()
                if name:
                    return name
    except Exception as e:
        logger.error("Failed resolving required assistant name from resource file: %s", e)
        logger.debug("required assistant name resolution exception details", exc_info=True)
        raise

    raise RuntimeError(
        "Assistant name is required but not configured. "
        "Set ASSISTANT_NAME or resources/assistant/resource_assistant_data.json:name."
    )


def get_required_primary_user_name() -> str:
    env_val = str(os.environ.get("PRIMARY_USER") or "").strip()
    if env_val:
        return env_val

    resource_path = get_resources_dir() / "user" / "resource_user_data.json"
    try:
        if resource_path.exists():
            payload = json.loads(resource_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                name = str(payload.get("preferred_name") or payload.get("first_name") or "").strip()
                if name:
                    return name
    except Exception as e:
        logger.error("Failed resolving required primary user name from resource file: %s", e)
        logger.debug("required primary user name resolution exception details", exc_info=True)
        raise

    raise RuntimeError(
        "Primary user name is required but not configured. "
        "Set PRIMARY_USER or resources/user/resource_user_data.json:preferred_name/first_name."
    )


def resolve_display_name(
    *,
    raw_name: str | None = None,
    role: str | None = None,
    direction: str | None = None,
    external_id: str | None = None,
    prefer_external_id_for_participant: bool = False,
) -> str:
    """
    Resolve a display name with strict identity semantics.

    Order:
      1. Use explicit `raw_name` when present.
      2. If assistant/outbound role is implied, use required assistant name.
      3. For participant/user roles, optionally prefer external_id, else required primary user name.
      4. For unknown role, prefer external_id when available, else required primary user name.
    """
    explicit = str(raw_name or "").strip()
    if explicit:
        return explicit

    normalized_role = str(role or "").strip().lower()
    normalized_direction = str(direction or "").strip().lower()
    normalized_external_id = str(external_id or "").strip()

    if normalized_role in {"assistant", get_required_assistant_name().lower(), "agent"} or normalized_direction == "outbound":
        return get_required_assistant_name()

    if normalized_role in {"user", "human", "owner", "participant"}:
        if prefer_external_id_for_participant and normalized_external_id:
            return normalized_external_id
        return get_required_primary_user_name()

    if normalized_external_id:
        return normalized_external_id

    return get_required_primary_user_name()
