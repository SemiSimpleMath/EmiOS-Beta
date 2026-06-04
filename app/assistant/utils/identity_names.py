from __future__ import annotations

import json
import os

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir

logger = get_logger(__name__)


# ── Canonical stable principal ids ───────────────────────────────────────────
# Returned by resolve_principal(). Name-neutral — they never contain a person's
# or the assistant's configured name. Use these for scoping / ownership /
# routing keys. For anything a human reads, use the *_name resolvers below.
PRINCIPAL_SELF = "self"   # the assistant acting as itself
PRINCIPAL_USER = "user"   # the primary human owner


def get_assistant_name() -> str:
    """Assistant display name, soft: ASSISTANT_NAME -> resource:name -> generic default.

    For human-facing display. Falls back to a name-neutral default rather than
    any specific persona name (origin is the public repo). Use
    get_required_assistant_name() when a missing config should fail loudly
    instead of silently defaulting.
    """
    env_val = os.environ.get("ASSISTANT_NAME", "").strip()
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
        logger.error("Could not load assistant name from resource file: %s", e, exc_info=True)
    return "Assistant"


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


_SELF_ALIASES = {"self", "me", "myself", "assistant", "herself", "himself", "themself"}
_USER_ALIASES = {"user", "", "owner", "primary", "primary_user"}


def resolve_principal(raw: str | None) -> str:
    """Canonicalize an acting_as / principal string to a stable form.

    Many surface inputs name the same principal: `/actas self`, `/actas me`,
    `/actas <assistant's configured name>` all mean "the assistant acting as
    herself". This maps every alias to a CANONICAL id so storage, the skill
    principal-gate, account `accessible_by`, and the principal skill-pack all
    compare on the same value — and none of them hardcode the install's
    assistant name.

    Canonical ids:
      "self"  — the assistant-as-persona (aliases: self/me/myself/assistant +
                the configured assistant name from resource_assistant_data).
      "user"  — the primary user / default (aliases: user/owner/primary/empty).
      "<id>"  — any other principal (e.g. a future persona "katy") passes
                through lowercased, so /actas katy -> "katy" works unchanged.

    Pure + cheap; safe to call on every comparison. Never raises — an
    unresolvable assistant-name lookup just means the configured name isn't
    treated as a self-alias (still correct for explicit "self").
    """
    val = str(raw or "").strip().lower()
    if val in _USER_ALIASES:
        return "user"
    if val in _SELF_ALIASES:
        return "self"
    # The configured assistant name is an alias of "self" (so an account's
    # accessible_by entry or an acting_as value that names the assistant both
    # canonicalize to "self" — regardless of how the install named its assistant).
    try:
        if val and val == get_required_assistant_name().strip().lower():
            return "self"
    except Exception:
        # No configured name available — fall through; explicit "self" still works.
        pass
    # The configured primary-user name is an alias of "user" (so acting_as naming
    # the owner canonicalizes to "user" without hardcoding the install's owner name).
    try:
        if val and val == get_required_primary_user_name().strip().lower():
            return "user"
    except Exception:
        pass
    return val


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
