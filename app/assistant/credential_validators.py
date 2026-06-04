"""Pre-mint live tests for credentials the /settings/accounts UI is about to stash.

Each validator takes the values the user pasted, runs a cheap auth probe
against the live API, and returns (ok, message). The configure route uses
this to refuse to mint a pod for a typo'd password before the user
discovers it later via a confusing 401.

Per-platform validators are dispatched by the `auth.validator` field on the
account's env-registry entry. Missing validator = skip the
probe and mint anyway.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Optional, Tuple

import requests

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def validate_bluesky_credential(
    handle: str,
    password: str,
    *,
    timeout_s: float = 10.0,
) -> Tuple[bool, str]:
    """Try createSession against bsky.social — proves both handle + app password.

    Returns (True, "Authenticated as <did>") on success; (False, reason) otherwise.
    Never returns the password or the resulting JWTs.
    """
    handle = (handle or "").strip()
    password = (password or "").strip()
    if not handle:
        return False, "Bluesky handle is empty"
    if not password:
        return False, "App password is empty"

    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    payload = {"identifier": handle, "password": password}
    try:
        resp = requests.post(
            url, json=payload, timeout=timeout_s,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        return False, f"Network error contacting bsky.social: {e}"

    if resp.status_code == 200:
        try:
            data = resp.json()
            did = data.get("did") or "(no did)"
            return True, f"Authenticated as {did}"
        except json.JSONDecodeError:
            return False, "Server returned 200 but body was not JSON"

    # Bluesky returns useful error bodies; surface them but don't echo the password back.
    try:
        body = resp.json()
        err = body.get("error") or "Unknown"
        msg = body.get("message") or "(no message)"
        return False, f"Bluesky rejected credentials: {err} — {msg}"
    except json.JSONDecodeError:
        return False, f"Bluesky returned HTTP {resp.status_code}"


# Validator registry. Key matches the `auth.validator` field in the resource.
VALIDATORS: Dict[str, Callable[..., Tuple[bool, str]]] = {
    "bluesky": validate_bluesky_credential,
}


def run_validator(
    validator_name: Optional[str],
    *,
    handle: str,
    secret: str,
) -> Tuple[bool, str]:
    """Look up the validator by name; skip silently (ok=True) if unknown.

    "skip silently" is intentional — adding a new account kind shouldn't
    require simultaneously writing a validator. The configure route shows
    a "no pre-mint probe available for this platform" note on the UI
    instead of failing.
    """
    name = (validator_name or "").strip().lower()
    if not name:
        return True, "no validator declared — minting without probe"
    fn = VALIDATORS.get(name)
    if not fn:
        return True, f"no validator registered for {name!r} — minting without probe"
    try:
        return fn(handle, secret)
    except Exception as e:
        logger.exception("Credential validator %r raised", name)
        return False, f"validator crashed: {type(e).__name__}: {e}"
