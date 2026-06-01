"""Unified registry of Emi's own accounts across platforms.

Subsystems read this when they need to act *as Emi* (vs. as the user).
The registry declares slots; handles come from EMI_OWN_* env vars at
read time so they stay out of git, and auth tokens live in either the
existing OAuth registry (Google accounts) or auth.* pods (everything
else). Schema at resources/resource_emi_accounts.json.

The principal that a scope executes under (`ScopeContext.acting_as`)
is controlled by the `/actas` slash command (see
``room_session_manager/services/actas_session_service.py``); this file
only owns the accounts registry and the assistant-self principal lookup.

Typical consumers:
- Subconscious: "what platforms does Emi have a presence on?"
- send_email / get_emails: when called with from_account="emi", which Google
  account_id should they use (vs. Jukka's google_user_primary)?
- Future bluesky_post / x_post / reddit_post tools: lookup handle + auth pod
  by platform before composing the call.
- room_slash_command_router: read `_assistant_self_principal()` to stamp
  the right principal name when `/actas self` is invoked.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_RESOURCE_REL = "resources/resource_emi_accounts.json"


@lru_cache(maxsize=1)
def _load_raw() -> Dict[str, Any]:
    path = get_repo_root() / _RESOURCE_REL
    if not path.exists():
        logger.warning("emi_accounts: %s missing; returning empty registry", path)
        return {"accounts": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def reload() -> None:
    """Drop the cache (useful after editing the resource on disk)."""
    _load_raw.cache_clear()


def _resolve_handle(account: Dict[str, Any]) -> str:
    env_name = str(account.get("handle_env") or "").strip()
    if not env_name:
        return ""
    return str(os.getenv(env_name) or "").strip()


def _resolve_auth_pod_id(account: Dict[str, Any]) -> str:
    auth = account.get("auth") or {}
    if (auth.get("kind") or "").strip() != "pod_ref":
        return ""
    env_name = str(auth.get("pod_id_env") or "").strip()
    if not env_name:
        return ""
    return str(os.getenv(env_name) or "").strip()


def _project(account: Dict[str, Any]) -> Dict[str, Any]:
    """Hydrate a raw account record with resolved handle + auth pod id.

    Returns a copy with `handle` and (for pod_ref auth) `auth.pod_id`
    fields populated from env at read time. `status` collapses to
    `unconfigured` if a configured account is missing any required
    env-supplied value (handle and, for pod_ref auth, pod_id) so the
    UI shows the truth instead of a misleading green check.
    """
    out = dict(account)
    handle = _resolve_handle(account)
    out["handle"] = handle
    auth = dict(account.get("auth") or {})
    auth_pod_id = ""
    if (auth.get("kind") or "") == "pod_ref":
        auth_pod_id = _resolve_auth_pod_id(account)
        auth["pod_id"] = auth_pod_id
    out["auth"] = auth
    declared = str(account.get("status") or "").strip().lower()
    if declared == "configured":
        missing = []
        if not handle:
            missing.append(f"handle env var {account.get('handle_env')!r}")
        if (auth.get("kind") or "") == "pod_ref" and not auth_pod_id:
            missing.append(f"auth pod id env var {auth.get('pod_id_env')!r}")
        if missing:
            out["status"] = "unconfigured"
            out["status_reason"] = (
                "resource declares status=configured but missing: " + ", ".join(missing)
            )
    return out


def get_emi_accounts() -> List[Dict[str, Any]]:
    """All accounts in resource order, with env-resolved handle + auth."""
    raw = _load_raw()
    return [_project(a) for a in (raw.get("accounts") or [])]


def get_emi_account(account_id: str) -> Optional[Dict[str, Any]]:
    """One account by id, with env-resolved fields. None if unknown."""
    aid = (account_id or "").strip()
    for a in get_emi_accounts():
        if str(a.get("id") or "").strip() == aid:
            return a
    return None


def get_emi_account_by_platform(platform: str) -> Optional[Dict[str, Any]]:
    """First account on the given platform. Convenience for single-account-per-platform consumers."""
    plat = (platform or "").strip().lower()
    for a in get_emi_accounts():
        if str(a.get("platform") or "").strip().lower() == plat:
            return a
    return None


def get_emi_handle(platform: str) -> str:
    """Handle for Emi on the given platform, or empty string if unset/unknown."""
    account = get_emi_account_by_platform(platform)
    if not account:
        return ""
    return str(account.get("handle") or "")


def get_accounts_for_principal(principal: str) -> List[Dict[str, Any]]:
    """Return only the accounts the given principal is allowed to use.

    Reads `accessible_by` (list of principal names) on each account; defaults
    to ["emi"] when missing (today every account in the registry is Emi's
    own). When acting_as is "user" the planner sees nothing here — the
    user-as-principal uses their primary accounts via the existing OAuth
    registry, not the Emi-owned overlay.
    """
    out: List[Dict[str, Any]] = []
    target = (principal or "").strip().lower() or "user"
    for a in get_emi_accounts():
        allowed = a.get("accessible_by") or ["emi"]
        allowed_lower = [str(p).strip().lower() for p in allowed if isinstance(p, str)]
        if target in allowed_lower:
            out.append(a)
    return out


def render_accounts_for_planner(principal: str) -> str:
    """Render the scope-filtered account list as a prompt-ready text block.

    Output shape (when principal=='emi', current registry):

        You are currently acting as: emi
        Accounts available to this scope (use these literal values in tool calls):
        - gmail: emi@example.com (Google OAuth account_id: emi_google_primary)
        - bluesky: emi.bsky.social
            secret ref (paste verbatim in tool body): datapod:auth.bearer:f97.../full

    Empty when no accounts apply (e.g. principal='user' today). The planner
    uses these literal values when composing tool calls that need a handle
    or a pod ref — no Python function calls or external resolution needed.
    The `/full` projection suffix on pod refs is what triggers http_request
    (or any pod-aware tool) to resolve the actual secret at courier scope;
    without it the literal pod_id goes through as a string and the API
    rejects it as a bad password.
    """
    accounts = get_accounts_for_principal(principal)
    if not accounts:
        return ""
    lines = [f"You are currently acting as: {principal}",
             "Accounts available to this scope (use these literal values in tool calls):"]
    for a in accounts:
        if str(a.get("status") or "").strip().lower() != "configured":
            continue
        platform = str(a.get("platform") or "")
        handle = str(a.get("handle") or "(no handle)")
        auth = a.get("auth") or {}
        if (auth.get("kind") or "") == "google_oauth":
            lines.append(
                f"- {platform}: {handle} (Google OAuth account_id: {auth.get('account_id', '')})"
            )
        elif (auth.get("kind") or "") == "pod_ref":
            pid = auth.get("pod_id") or ""
            if pid:
                lines.append(f"- {platform}: {handle}")
                lines.append(
                    f"    secret ref (paste verbatim in tool body): {pid}/full"
                )
            else:
                lines.append(f"- {platform}: {handle}")
        else:
            lines.append(f"- {platform}: {handle}")
    if len(lines) == 2:  # only the two header lines, no configured accounts
        return ""
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _assistant_self_principal() -> str:
    """The principal name that represents the assistant acting as herself.

    Sourced from ``resource_assistant_data.name`` (Jukka's install: "emi";
    other installs: whatever they named their assistant). Lowercased. This
    is the name used both as the @-token (`@<assistant_name>`) and as the
    `acting_as` value stamped on scope.

    The name is read from disk once and cached; rename requires restart
    (intentional — same discipline as other resource loads).
    """
    try:
        path = get_repo_root() / "resources" / "assistant" / "resource_assistant_data.json"
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        name = str(data.get("name") or "").strip()
        if name:
            return name.lower()
    except Exception:
        logger.warning("emi_accounts: could not read assistant name; falling back to 'emi'")
    return "emi"


def resolve_gmail_account_id(
    alias: Optional[str],
    *,
    scope_acting_as: Optional[str] = None,
) -> str:
    """Resolve a planner-friendly alias to a Google OAuth account_id.

    Two layers of input, in priority order:

        1. Explicit per-call `alias` — what the planner passed as
           `from_account` to send_email. Wins over scope default.
        2. Scope-level `scope_acting_as` — the principal this task is
           executing under (from ScopeContext.acting_as, stamped at ingress
           by the keyword detector or by routines/pipelines).

    Both go through the same alias resolution table:

        "jukka" / "user" / "owner" / ""   → Jukka's primary Google account
                                            (from EMI_GOOGLE_GMAIL_ACCOUNT_ID).
        "emi"                              → Emi's own Gmail account, via the
                                            platform=gmail record in
                                            resource_emi_accounts.json.
        <other account_id>                 → passed through as a literal
                                            (must exist in oauth_accounts.json).

    The principal-name table will grow as we register additional principals
    (Katy, Peter, an external client). Each new principal needs a matching
    entry in resource_emi_accounts.json (platform=gmail) for this resolver
    to map it to an account_id.

    Raises ValueError with an actionable message when "emi" is requested but
    the registry has no Gmail record for her, or when the resolved record's
    auth.kind isn't google_oauth, or when her account is unconfigured.
    """
    explicit = (alias or "").strip().lower()
    effective = explicit or (scope_acting_as or "").strip().lower()
    raw = effective
    if raw in ("", "jukka", "user", "owner"):
        from app.assistant.lib.google_auth.account_ids import GMAIL_GOOGLE_ACCOUNT_ID
        return GMAIL_GOOGLE_ACCOUNT_ID
    if raw == "emi":
        account = get_emi_account_by_platform("gmail")
        if not account:
            raise ValueError(
                "acting_as='emi' (or from_account='emi') requested but "
                "resource_emi_accounts.json has no record with platform='gmail'."
            )
        auth = account.get("auth") or {}
        if (auth.get("kind") or "").strip() != "google_oauth":
            raise ValueError(
                f"acting_as='emi' requested but {account['id']!r} has "
                f"auth.kind={auth.get('kind')!r}; expected 'google_oauth'."
            )
        if str(account.get("status") or "").strip().lower() != "configured":
            reason = account.get("status_reason") or (
                f"set {account.get('handle_env')!r} in .env, drop OAuth client at "
                f"credentials/emi_google.json, then complete /oauth/google/start"
                f"?account_id={auth.get('account_id')}"
            )
            raise ValueError(
                f"acting_as='emi' requested but {account['id']!r} is not "
                f"configured: {reason}"
            )
        return str(auth.get("account_id") or "")
    return effective
