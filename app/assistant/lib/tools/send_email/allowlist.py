"""Email allowlist — recipients send_email can send to autonomously.

Loaded once at first use from `configs/email_allowlist.yaml`. The file
is a flat list of lowercase email strings under a `recipients:` key.
Editing the file requires a Flask restart to take effect (we cache the
loaded set in module memory).

This module is consulted by `SendEmail.compute_approval_reduction` —
when the `to` argument matches an entry, the standard approval gate is
softened so the dayflow_orchestrator (authority 95) can email
allowlisted recipients without firing a confirmation ticket. Recipients
NOT on the list still trigger the normal approval flow.

Matching is case-insensitive exact match. No wildcards. No substring.
The allowlist file is user-edited only — never written by Emi or any
agent path.
"""
from __future__ import annotations

from typing import Optional, Set

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_CACHE: Optional[Set[str]] = None


def _allowlist_path():
    return get_repo_root() / "configs" / "email_allowlist.yaml"


def load_email_allowlist() -> Set[str]:
    """Return the set of lowercase email addresses on the allowlist.

    Cached after first call. Use `invalidate_email_allowlist()` to force
    a reload (rarely needed — admin tooling, tests).
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _allowlist_path()
    if not path.exists():
        logger.warning(
            "email allowlist: %s not found — no recipients will bypass approval",
            path,
        )
        _CACHE = set()
        return _CACHE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.error("email allowlist: failed to parse %s: %s", path, e)
        _CACHE = set()
        return _CACHE
    recipients = data.get("recipients") or []
    if not isinstance(recipients, list):
        logger.error(
            "email allowlist: %s `recipients:` is not a list (got %s); "
            "treating as empty",
            path, type(recipients).__name__,
        )
        _CACHE = set()
        return _CACHE
    cleaned: Set[str] = set()
    for entry in recipients:
        if isinstance(entry, str):
            s = entry.strip().lower()
            if s and "@" in s:
                cleaned.add(s)
    _CACHE = cleaned
    logger.info(
        "email allowlist: loaded %d recipients from %s",
        len(_CACHE), path,
    )
    return _CACHE


def is_allowlisted(email: str) -> bool:
    """Case-insensitive exact-match check against the allowlist."""
    if not email or not isinstance(email, str):
        return False
    return email.strip().lower() in load_email_allowlist()


def invalidate_email_allowlist() -> None:
    """Drop the cached set; next load_email_allowlist() re-reads from disk."""
    global _CACHE
    _CACHE = None
