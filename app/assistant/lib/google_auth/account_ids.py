from __future__ import annotations

import os

from app.assistant.lib.google_auth import oauth_registry
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _env(name: str, default: str) -> str:
    value = str(os.getenv(name, default) or "").strip()
    if not value:
        raise ValueError(f"{name} resolved to empty value.")
    return value


def _validated(env_name: str, default: str) -> str:
    value = _env(env_name, default)
    if not oauth_registry.is_known_account(value):
        source = "environment" if os.getenv(env_name) else "default"
        if source == "default":
            # The account simply isn't configured yet — normal on a fresh install, or
            # for an optional integration (e.g. Nest). Defer real validation to actual
            # use (load_google_credentials raises a clear "authenticate" error there);
            # don't crash boot at import time.
            logger.warning("OAuth default account %r not configured yet — deferring.", value)
            return value
        known = sorted(oauth_registry.list_accounts().keys())
        raise RuntimeError(
            f"{env_name}={value!r} (from environment) is not in the OAuth registry. "
            f"Add it to oauth_accounts.json or unset the var. Known accounts: {known}."
        )
    return value


DEFAULT_GOOGLE_ACCOUNT_ID = _validated("EMI_GOOGLE_DEFAULT_ACCOUNT_ID", "google_user_primary")
GMAIL_GOOGLE_ACCOUNT_ID = _validated("EMI_GOOGLE_GMAIL_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
CALENDAR_GOOGLE_ACCOUNT_ID = _validated("EMI_GOOGLE_CALENDAR_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
TASKS_GOOGLE_ACCOUNT_ID = _validated("EMI_GOOGLE_TASKS_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
NEST_GOOGLE_ACCOUNT_ID = _validated("EMI_GOOGLE_NEST_ACCOUNT_ID", "google_nest")

