"""
Shared Google OAuth credential loader.

Canonical credential location (single source of truth):
  - OAuth client files: driven by OAuthRegistry (configs/oauth_accounts.json)
  - OAuth user tokens: database table google_oauth_accounts (encrypted)

All Google service clients (Gmail/Calendar/Tasks/Nest) should use this module.
"""

from __future__ import annotations

import os
import time
import threading
import webbrowser
import json
from typing import Sequence

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.assistant.lib.google_auth.account_ids import DEFAULT_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth import oauth_registry
from app.assistant.lib.google_auth.oauth_account_store import get_google_oauth_account_store
from app.assistant.lib.google_auth.principal_resolver import fetch_principal_email
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_oauth_browser_last_opened: dict[str, float] = {}
_oauth_browser_lock = threading.Lock()
# One tab per account per hour. This MUST exceed the routine auto-recovery probe
# spacing: at 120s it never suppressed anything, because the thing that fails and
# re-opens is a routine probe minutes apart — so every probe spawned another consent
# tab regardless of what the user did in the previous one (2026-08-23: tabs at 21:32
# and 22:02 for the same account, which read as "it keeps asking and never takes").
_OAUTH_BROWSER_COOLDOWN_S = 3600


def _open_oauth_browser(account_id: str) -> None:
    """Best-effort: open the OAuth consent flow in the user's browser (debounced)."""
    try:
        if os.getenv("EMI_AUTO_OPEN_GOOGLE_OAUTH", "1").strip() in {"0", "false", "False"}:
            return
        now = time.monotonic()
        with _oauth_browser_lock:
            last = _oauth_browser_last_opened.get(account_id, 0.0)
            if now - last < _OAUTH_BROWSER_COOLDOWN_S:
                logger.debug(
                    "OAuth browser suppressed for account_id=%s (%.0fs since last open)",
                    account_id,
                    now - last,
                )
                return
            _oauth_browser_last_opened[account_id] = now
        base = (os.getenv("EMI_WEB_BASE_URL") or os.getenv("EMI_BASE_URL") or "http://127.0.0.1:8000").strip().rstrip("/")
        scope_set = oauth_registry.get_scope_set(account_id) if oauth_registry.is_known_account(account_id) else "workspace"
        oauth_url = (
            f"{base}/oauth/google/start?redirect_to=google_oauth_settings"
            f"&account_id={account_id}&scope_set={scope_set}"
        )
        webbrowser.open(oauth_url, new=2)
        logger.info("Opened OAuth re-auth browser for account_id=%s scope_set=%s", account_id, scope_set)
    except Exception as e:
        logger.error("Failed opening OAuth re-auth URL for account_id=%s: %s", account_id, e)
        logger.debug("OAuth browser open exception details", exc_info=True)


def load_google_credentials(
    *,
    account_id: str = DEFAULT_GOOGLE_ACCOUNT_ID,
    required_scopes: Sequence[str],
    allow_refresh: bool = True,
    expected_principal_email: str | None = None,
) -> Credentials:
    """
    Load OAuth credentials from encrypted account store and ensure required scopes are present.

    This module intentionally does NOT run interactive OAuth flows. The user must
    authenticate via the app's Google OAuth UI, which writes account-scoped credentials.
    """
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("account_id is required for Google OAuth credential loading.")

    store = get_google_oauth_account_store()
    stored = store.get_credentials(account_id=account_id)
    if stored is None:
        _open_oauth_browser(account_id)
        raise FileNotFoundError(
            f"Google OAuth credentials not found for account_id={account_id!r}. "
            "Authenticate via the app Google OAuth flow for that account."
        )

    # Identity guard: a configured account must authenticate as the identity it
    # CLAIMS. Refuse if the consented principal_email mismatches the expected one
    # (e.g. the assistant's account consented as the user's Gmail instead of her own), so a
    # mis-consented account can't silently send as the wrong person.
    # See docs/architecture/SECRETS_ACCOUNTS.md.
    if expected_principal_email:
        actual = str(stored.get("principal_email") or "").strip()
        if actual and actual.lower() != str(expected_principal_email).strip().lower():
            raise PermissionError(
                f"account_id={account_id!r} is authenticated as {actual!r} but should be "
                f"{expected_principal_email!r}. Re-consent at "
                f"/oauth/google/start?account_id={account_id} signed in as {expected_principal_email}."
            )

    raw_json = str(stored.get("credentials_json") or "").strip()
    if not raw_json:
        raise ValueError(f"Stored OAuth credentials are empty for account_id={account_id!r}.")
    try:
        info = json.loads(raw_json)
    except Exception as e:
        logger.error("Stored OAuth JSON parse failed for account_id=%s: %s", account_id, e)
        logger.debug("Stored OAuth JSON parse exception details", exc_info=True)
        raise ValueError(f"Stored OAuth JSON is invalid for account_id={account_id!r}.") from e
    if not isinstance(info, dict):
        raise ValueError(f"Stored OAuth JSON must be an object for account_id={account_id!r}.")
    creds = Credentials.from_authorized_user_info(info)

    if not isinstance(creds, Credentials):
        raise TypeError(
            f"Invalid credential format for account_id={account_id!r} "
            "(expected google.oauth2.credentials.Credentials)."
        )

    # Scope validation: ensure required scopes are granted.
    granted = set(creds.scopes or [])
    missing = [s for s in required_scopes if s not in granted]
    if missing:
        _open_oauth_browser(account_id)
        raise PermissionError(
            "Google OAuth token is missing required scopes: "
            f"{missing}. Re-authenticate and grant all requested permissions."
        )

    if not creds.valid:
        if allow_refresh and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed_json = creds.to_json()
                principal = str(stored.get("principal_email") or "").strip() or None
                if principal is None:
                    principal = fetch_principal_email(creds)
                store.upsert_credentials(
                    account_id=account_id,
                    principal_email=principal,
                    granted_scopes=list(creds.scopes or []),
                    credentials_json=refreshed_json,
                )
            except Exception as e:
                _open_oauth_browser(account_id)
                raise PermissionError(
                    "Google OAuth token refresh failed (token expired/revoked). "
                    "Re-authenticate via the app Google OAuth flow. "
                    f"Tip: open /oauth/google/start?account_id={account_id} in your browser."
                ) from e
        else:
            _open_oauth_browser(account_id)
            raise PermissionError(
                f"Google OAuth token for account_id={account_id!r} is missing/expired and cannot be refreshed. "
                "Re-authenticate via the app."
            )

    return creds

