"""
OAuth Account Registry — single source of truth for all OAuth integrations.

Loads account definitions from configs/oauth_accounts.json and provides
lookup helpers for credential paths, scopes, scope_set, and provider.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

def _lib_dir() -> Path:
    """app/assistant/lib — client_credentials paths in oauth_accounts.json
    are stored relative to this directory."""
    from app.assistant.utils.path_utils import get_app_root
    return get_app_root() / "assistant" / "lib"


def _config_path() -> Path:
    """Live oauth_accounts.json under the writable configs dir (honors EMI_DATA_DIR).
    Computed lazily — not at import — so a launcher that sets EMI_DATA_DIR before
    create_app is respected, and the packaged install reads from its data dir."""
    from app.assistant.utils.path_utils import get_configs_dir
    return get_configs_dir() / "oauth_accounts.json"

_cache: Dict[str, Any] | None = None


def _load_config() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = _config_path()
    if not path.exists():
        # Normal on a fresh install before seeding / if Google was never set up.
        # No accounts configured — degrade gracefully rather than crash boot.
        logger.info("No oauth_accounts.json yet (%s) — no OAuth accounts configured.", path)
        _cache = {"accounts": {}}
        return _cache
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or "accounts" not in data:
        raise ValueError(f"oauth_accounts.json must contain an 'accounts' key.")
    accounts = data["accounts"]
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("oauth_accounts.json 'accounts' must be a non-empty object.")
    for account_id, cfg in accounts.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Account '{account_id}' config must be a JSON object.")
        for required in ("provider", "client_credentials", "scopes"):
            if required not in cfg:
                raise ValueError(
                    f"Account '{account_id}' missing required key '{required}'."
                )
    _cache = data
    logger.info(
        "OAuth registry loaded: %d account(s) — %s",
        len(accounts),
        ", ".join(sorted(accounts)),
    )
    return _cache


def reload() -> None:
    """Force-reload the config from disk (useful after config edits)."""
    global _cache
    _cache = None
    _load_config()


def get_account(account_id: str) -> Dict[str, Any]:
    """Return the full config dict for an account, or raise if unknown."""
    aid = str(account_id or "").strip()
    if not aid:
        raise ValueError("account_id is required.")
    config = _load_config()
    account = config["accounts"].get(aid)
    if account is None:
        raise KeyError(
            f"Unknown OAuth account_id '{aid}'. "
            f"Known accounts: {sorted(config['accounts'])}."
        )
    return dict(account)


def get_client_credentials_path(account_id: str) -> Path:
    """Resolve the absolute path to the OAuth client credentials file for an account."""
    account = get_account(account_id)
    rel_path = str(account["client_credentials"]).strip()
    if not rel_path:
        raise ValueError(f"Account '{account_id}' has empty client_credentials path.")
    resolved = _lib_dir() / rel_path
    if not resolved.exists():
        raise FileNotFoundError(
            f"OAuth client credentials file not found for account '{account_id}': {resolved}. "
            f"Download it from Google Cloud Console and place it at: {resolved}"
        )
    return resolved


def get_scopes(account_id: str) -> List[str]:
    """Return the scope list for an account."""
    account = get_account(account_id)
    scopes = account.get("scopes", [])
    if not isinstance(scopes, list) or not scopes:
        raise ValueError(f"Account '{account_id}' has no scopes defined.")
    return list(scopes)


def get_scope_set(account_id: str) -> str:
    """Return the scope_set name for an account (e.g. 'workspace', 'nest')."""
    account = get_account(account_id)
    scope_set = str(account.get("scope_set") or "").strip()
    if not scope_set:
        raise ValueError(f"Account '{account_id}' has no scope_set defined.")
    return scope_set


def get_provider(account_id: str) -> str:
    """Return the provider name for an account (e.g. 'google', 'microsoft')."""
    account = get_account(account_id)
    return str(account.get("provider") or "").strip()


def get_display_name(account_id: str) -> str:
    """Return the human-readable display name for an account."""
    account = get_account(account_id)
    return str(account.get("display_name") or account_id).strip()


def list_accounts() -> Dict[str, Dict[str, Any]]:
    """Return all configured accounts as {account_id: config}."""
    config = _load_config()
    return dict(config["accounts"])


def get_nest_project_id(account_id: str) -> str | None:
    """Return the Nest/Device Access project ID for an account, or None if not applicable.

    Reads the env var named in the account's ``nest_project_id_env`` field.
    """
    account = get_account(account_id)
    env_key = str(account.get("nest_project_id_env") or "").strip()
    if not env_key:
        return None
    value = str(os.getenv(env_key) or "").strip()
    if not value:
        raise ValueError(
            f"Account '{account_id}' requires env var {env_key} for Nest project ID, "
            "but it is not set or is empty."
        )
    return value


def is_known_account(account_id: str) -> bool:
    """Check if an account_id is in the registry (without raising)."""
    try:
        config = _load_config()
        return str(account_id or "").strip() in config["accounts"]
    except Exception:
        return False
