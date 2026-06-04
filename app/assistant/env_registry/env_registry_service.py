"""Unified env-var registry — read + account-resolution service.

Loads BUILTIN entries (shipped, tracked: builtins.json) merged with USER entries
(gitignored, UI-written: resources/env_registry_user.json) and exposes lookups for
the central settings UI, inline feature-page sections, account resolution (folded in
from the retired emi_accounts.py), and env var-name indirection.

Metadata only: actual VALUES are never stored here — plain values live in .env
(kind=value), secrets in pods (kind=secret / account.auth). This holds the env var
NAMES + display metadata so code stops hardcoding names like EMI_OWN_GMAIL_HANDLE.

No guessed defaults: a missing entry/field raises (the caller decides), per the
project's fail-loud rule. (Empty string for an UNSET handle/secret is a real
"not configured yet" value, not a guessed identity.)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from app.assistant.utils.identity_names import get_required_assistant_name, resolve_principal
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root, get_resources_dir

logger = get_logger(__name__)

_BUILTINS_PATH = get_app_root() / "assistant" / "env_registry" / "builtins.json"
_USER_PATH = get_resources_dir() / "env_registry_user.json"


class EnvRegistryService:
    """Read-only view over builtin + user env-var registry entries, plus the
    account projection/resolution that used to live in emi_accounts.py."""

    # ── core entry reads ──────────────────────────────────────────────────────
    def _load(self, path: Path, *, required: bool) -> list[dict[str, Any]]:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"env registry: required file missing: {path}")
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError(f"env registry {path.name}: top-level 'entries' list is required")
        return [e for e in data["entries"] if isinstance(e, dict) and str(e.get("name") or "").strip()]

    def entries(self) -> list[dict[str, Any]]:
        """Builtins (shipped) merged with user entries; user `name` wins on collision."""
        by_name: dict[str, dict[str, Any]] = {}
        for e in self._load(_BUILTINS_PATH, required=True):
            by_name[str(e["name"])] = {**e, "builtin": True}
        for e in self._load(_USER_PATH, required=False):
            by_name[str(e["name"])] = {**e, "builtin": False}
        return list(by_name.values())

    def get(self, name: str) -> Optional[dict[str, Any]]:
        for e in self.entries():
            if str(e.get("name")) == name:
                return e
        return None

    def for_feature(self, feature: str) -> list[dict[str, Any]]:
        f = str(feature or "").strip().lower()
        return [e for e in self.entries() if str(e.get("feature") or "").strip().lower() == f]

    def for_owner(self, owner: str) -> list[dict[str, Any]]:
        o = str(owner or "").strip().lower()
        return [e for e in self.entries() if str(e.get("owner") or "").strip().lower() == o]

    def account_env_name(self, name: str, *, field: str = "handle_env") -> str:
        """Resolve the env var NAME for an account entry's field.

        field="handle_env" -> the handle env var; otherwise reads auth.<field>
        (e.g. "env_ref", "pod_id_env"). Raises if the entry/field is absent.
        """
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"env registry: no entry named {name!r}")
        value = entry.get("handle_env") if field == "handle_env" else (entry.get("auth") or {}).get(field)
        if not value:
            raise KeyError(f"env registry: entry {name!r} has no {field!r}")
        return str(value)

    # ── account projection + resolution (ported from emi_accounts.py) ─────────
    def _resolve_handle(self, entry: dict[str, Any]) -> str:
        env_name = str(entry.get("handle_env") or "").strip()
        return str(os.getenv(env_name) or "").strip() if env_name else ""

    def _resolve_auth_pod_id(self, entry: dict[str, Any]) -> str:
        auth = entry.get("auth") or {}
        if (auth.get("kind") or "").strip() != "pod_ref":
            return ""
        env_name = str(auth.get("pod_id_env") or "").strip()
        return str(os.getenv(env_name) or "").strip() if env_name else ""

    def _project_account(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Hydrate an account entry with env-resolved handle + auth pod id, and
        derive `status` from whether the env-supplied values are present (so the
        UI/planner sees the truth instead of a stored, possibly-stale flag)."""
        handle = self._resolve_handle(entry)
        auth = dict(entry.get("auth") or {})
        auth_pod_id = ""
        if (auth.get("kind") or "") == "pod_ref":
            auth_pod_id = self._resolve_auth_pod_id(entry)
            auth["pod_id"] = auth_pod_id
        configured = bool(handle) and ((auth.get("kind") or "") != "pod_ref" or bool(auth_pod_id))
        return {
            "id": str(entry.get("name") or ""),
            "platform": entry.get("platform"),
            "display_name": entry.get("label"),
            "feature": entry.get("feature"),
            "owner": entry.get("owner"),
            "accessible_by": entry.get("accessible_by") or ["self"],
            "handle": handle,
            "handle_env": entry.get("handle_env"),
            "auth": auth,
            "status": "configured" if configured else "unconfigured",
        }

    def accounts(self) -> list[dict[str, Any]]:
        """All account entries, projected (env-resolved handle/pod + derived status)."""
        return [self._project_account(e) for e in self.entries() if e.get("kind") == "account"]

    def account(self, account_id: str) -> Optional[dict[str, Any]]:
        aid = (account_id or "").strip()
        for a in self.accounts():
            if a["id"] == aid:
                return a
        return None

    def account_by_platform(self, platform: str) -> Optional[dict[str, Any]]:
        plat = (platform or "").strip().lower()
        for a in self.accounts():
            if str(a.get("platform") or "").strip().lower() == plat:
                return a
        return None

    def handle(self, platform: str) -> str:
        a = self.account_by_platform(platform)
        return str(a.get("handle") or "") if a else ""

    def accounts_for_principal(self, principal: str) -> list[dict[str, Any]]:
        """Projected account entries whose accessible_by includes the (canonicalized) principal."""
        target = resolve_principal(principal)
        out: list[dict[str, Any]] = []
        for a in self.accounts():
            allowed = a.get("accessible_by") or []
            if any(resolve_principal(str(p)) == target for p in allowed if isinstance(p, str)):
                out.append(a)
        return out

    def assistant_self_principal(self) -> str:
        """The principal name representing the assistant acting as herself
        (configured assistant name, lowercased). Fails loud if unconfigured."""
        return get_required_assistant_name().lower()

    def render_accounts_for_planner(self, principal: str) -> str:
        """Scope-filtered account list as a prompt-ready text block (literal handles
        + Google account_id / pod ref). Empty when no configured accounts apply."""
        accounts = self.accounts_for_principal(principal)
        if not accounts:
            return ""
        lines = [
            f"You are currently acting as: {principal}",
            "Accounts available to this scope (use these literal values in tool calls):",
        ]
        for a in accounts:
            if str(a.get("status") or "").strip().lower() != "configured":
                continue
            platform = str(a.get("platform") or "")
            handle = str(a.get("handle") or "(no handle)")
            auth = a.get("auth") or {}
            kind = (auth.get("kind") or "")
            if kind == "google_oauth":
                lines.append(f"- {platform}: {handle} (Google OAuth account_id: {auth.get('account_id', '')})")
            elif kind == "pod_ref" and auth.get("pod_id"):
                lines.append(f"- {platform}: {handle}")
                lines.append(f"    secret ref (paste verbatim in tool body): {auth.get('pod_id')}/full")
            else:
                lines.append(f"- {platform}: {handle}")
        if len(lines) == 2:  # only headers, no configured accounts
            return ""
        return "\n".join(lines)

    def resolve_gmail_account_id(self, alias: Optional[str], *, scope_acting_as: Optional[str] = None) -> str:
        """Resolve a planner alias / scope principal to a Google OAuth account_id.

        explicit alias wins over scope; both canonicalize via resolve_principal:
        "user" -> the owner's primary Google account; "self" -> the assistant's
        gmail account_id; any other -> passed through as a literal account_id.
        Raises (no guess) when self is requested but her gmail isn't configured.
        """
        explicit = (alias or "").strip().lower()
        effective = explicit or (scope_acting_as or "").strip().lower()
        canonical = resolve_principal(effective)
        if canonical == "user":
            from app.assistant.lib.google_auth.account_ids import GMAIL_GOOGLE_ACCOUNT_ID
            return GMAIL_GOOGLE_ACCOUNT_ID
        if canonical == "self":
            account = self.account_by_platform("gmail")
            if not account:
                raise ValueError(
                    "acting_as=self (the assistant) requested but no account entry has platform='gmail'."
                )
            auth = account.get("auth") or {}
            if (auth.get("kind") or "").strip() != "google_oauth":
                raise ValueError(
                    f"acting_as=self requested but {account['id']!r} has "
                    f"auth.kind={auth.get('kind')!r}; expected 'google_oauth'."
                )
            if str(account.get("status") or "").strip().lower() != "configured":
                raise ValueError(
                    f"acting_as=self requested but {account['id']!r} is not configured: set "
                    f"{account.get('handle_env')!r} in .env, drop the OAuth client at "
                    f"credentials/emi_google.json, then complete "
                    f"/oauth/google/start?account_id={auth.get('account_id')}"
                )
            return str(auth.get("account_id") or "")
        return effective

    # ── .env file overview (central settings page) ────────────────────────────
    def _registry_meta_by_env_name(self) -> dict[str, dict[str, Any]]:
        """Map every env var NAME the registry knows about -> display metadata
        {label, hint, feature, owner}. Covers value/secret entries (the name IS
        the env var) and account entries (their handle_env / auth.env_ref /
        auth.pod_id_env). Used purely to overlay nicer labels on the real .env."""
        meta: dict[str, dict[str, Any]] = {}
        for e in self.entries():
            owner = str(e.get("owner") or "other")
            feature = str(e.get("feature") or "general")
            label = str(e.get("label") or e.get("name"))
            kind = e.get("kind")
            if kind in ("value", "secret"):
                meta[str(e["name"])] = {"label": label, "hint": str(e.get("hint") or ""),
                                        "feature": feature, "owner": owner}
            elif kind == "account":
                auth = e.get("auth") or {}
                hv = str(e.get("handle_env") or "").strip()
                if hv:
                    meta[hv] = {"label": f"{label} — handle", "hint": "",
                                "feature": feature, "owner": owner}
                er = str(auth.get("env_ref") or "").strip()
                if er:
                    meta[er] = {"label": f"{label} — {auth.get('secret_label') or 'secret'}",
                                "hint": str(auth.get("secret_hint") or ""),
                                "feature": feature, "owner": owner}
                pe = str(auth.get("pod_id_env") or "").strip()
                if pe:
                    meta[pe] = {"label": f"{label} — auth pod id", "hint": "",
                                "feature": feature, "owner": owner}
        return meta

    def env_overview(self) -> list[dict[str, Any]]:
        """The ACTUAL .env file as grouped, all-masked rows. The source of truth
        is the file, so a variable the user has not set never appears. Registry
        labels/features are overlaid where a name is known; unknown vars land
        under 'other'. Shape matches the central settings template:
        [{owner, features: [{feature, entries: [row]}]}]."""
        from dotenv import dotenv_values
        from app.assistant.utils.path_utils import get_repo_root
        env_path = get_repo_root() / ".env"
        file_vars = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
        meta = self._registry_meta_by_env_name()
        by_owner: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for name, value in file_vars.items():
            m = meta.get(name)
            owner = (m or {}).get("owner") or "other"
            feature = (m or {}).get("feature") or "other"
            row = {
                "name": name,
                "kind": "secret",                       # everything masked
                "label": (m or {}).get("label") or name,
                "hint": (m or {}).get("hint") or "",
                "owner": owner,
                "feature": feature,
                "builtin": True,                        # suppress the "custom" badge here
                "state": {"set": bool(value), "shown": "••••••" if value else ""},
            }
            by_owner.setdefault(owner, {}).setdefault(feature, []).append(row)
        order = ["user", "assistant"] + [o for o in by_owner if o not in ("user", "assistant")]
        groups: list[dict[str, Any]] = []
        for owner in order:
            if owner not in by_owner:
                continue
            feats = [{"feature": f, "entries": sorted(items, key=lambda r: r["name"])}
                     for f, items in sorted(by_owner[owner].items())]
            groups.append({"owner": owner, "features": feats})
        return groups

    # ── writes + display state (Phase 3 UI) ───────────────────────────────────
    def value_state(self, name: str) -> dict[str, Any]:
        """Display state for an inline value/secret entry: {set: bool, shown: str}.
        Every value is masked — .env is treated as all-secret, never echoed back."""
        entry = self.get(name)
        if entry is None:
            raise KeyError(f"env registry: no entry named {name!r}")
        current = str(os.getenv(name) or "")
        return {"set": bool(current), "shown": "••••••" if current else ""}

    def set_value(self, name: str, value: str) -> None:
        """Persist a plain .env variable (and process env). Account entries use
        the dedicated configure flow; everything else — a registry value/secret
        OR an unregistered .env var — is written straight to .env."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("env registry: a variable name is required")
        entry = self.get(name)
        if entry is not None and entry.get("kind") == "account":
            raise ValueError(f"env registry: {name!r} is an account; use the account configure flow")
        from app.assistant.utils.env_writer import upsert_env
        upsert_env(name, value)
        os.environ[name] = value

    def add_user_entry(self, *, name: str, owner: str = "user", feature: str = "general",
                       label: str = "", hint: str = "", secret: bool = False) -> dict[str, Any]:
        """Append a user-defined entry to the gitignored user registry file."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("env registry: entry name is required")
        if self.get(name) is not None:
            raise ValueError(f"env registry: an entry named {name!r} already exists")
        entry = {
            "name": name,
            "kind": "secret" if secret else "value",
            "owner": str(owner or "user").strip() or "user",
            "feature": str(feature or "general").strip() or "general",
            "label": str(label or name).strip() or name,
            "hint": str(hint or "").strip(),
        }
        data: dict[str, Any] = {"schema_version": 1, "entries": []}
        if _USER_PATH.exists():
            loaded = json.loads(_USER_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                data = loaded
        data["entries"].append(entry)
        _USER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return entry
