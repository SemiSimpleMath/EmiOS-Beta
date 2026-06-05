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
import re
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
        """Builtins (shipped) merged with user entries. User fields OVERRIDE the
        builtin's fields PER-FIELD (so a user override can add just `sensitivity`
        or `pod_id` without dropping the builtin's label/feature)."""
        builtin_names: set[str] = set()
        by_name: dict[str, dict[str, Any]] = {}
        for e in self._load(_BUILTINS_PATH, required=True):
            n = str(e["name"]); builtin_names.add(n)
            by_name[n] = dict(e)
        for e in self._load(_USER_PATH, required=False):
            n = str(e["name"])
            by_name[n] = {**by_name.get(n, {}), **e}
        return [{**v, "builtin": (k in builtin_names)} for k, v in by_name.items()]

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
        # Direct pod_id on the entry (user-created accounts) wins; else the legacy
        # pod_id_env convention (builtin accounts that stash the id in a .env var).
        direct = str(auth.get("pod_id") or "").strip()
        if direct:
            return direct
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
            "authority": int(entry.get("authority") or 99),  # surfacing dial; 99 = typical
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

    def _render_account_lines(self, principal: str, accounts: list[dict[str, Any]]) -> str:
        """Render a list of projected accounts as a prompt-ready text block (literal
        handles + Google account_id / pod ref). Empty when none are configured."""
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

    def render_accounts_for_planner(self, principal: str) -> str:
        """Principal-filtered account text (no authority filter). Prefer
        render_accounts_for_scope (the resource provider) which also applies the
        per-account authority dial."""
        return self._render_account_lines(principal, self.accounts_for_principal(principal))

    def _category_for_platform(self, platform: Any) -> str:
        """Domain category for an account platform (email / social / ...), per
        _FEATURE_BY_PLATFORM. Drives the resource_<category>_accounts relevance
        filter. Unknown platforms fall to 'other' so they only ever surface in
        the unfiltered (categories=None) view."""
        return self._FEATURE_BY_PLATFORM.get(str(platform or "").strip().lower(), "other")

    def render_accounts_for_scope(self, scope: Any, *, categories: Optional[set] = None) -> str:
        """Provider for the `resource_accounts` family of dynamic resources.
        Computes the principal- AND per-account-authority-filtered account text
        from the live scope: principal = acting_as; an account appears only if the
        caller's authority clears the account's `authority` dial.

        ``categories`` (optional) further restricts to accounts whose platform
        category (gmail->email, bluesky->social, …) is in the set — the DOMAIN
        relevance filter. An email/calendar agent requests {"email"} so social
        accounts never enter its prompt; None = no domain filter (every category).
        This is RELEVANCE, not permission: scope + authority already gate access;
        categories only gate what's pertinent to the agent's job.
        See docs/architecture/SECRETS_ACCOUNTS.md."""
        if hasattr(scope, "model_dump"):
            scope = scope.model_dump()
        if not isinstance(scope, dict):
            scope = {}
        principal = str(scope.get("acting_as") or "user")
        approval = scope.get("approval")
        if isinstance(approval, dict):
            caller_authority = int(approval.get("authority_level") or 0)
        else:
            caller_authority = int(getattr(approval, "authority_level", 0) or 0)
        accounts = [
            a for a in self.accounts_for_principal(principal)
            if int(a.get("authority") or 99) <= caller_authority
        ]
        if categories is not None:
            wanted = {str(c).strip().lower() for c in categories if str(c).strip()}
            accounts = [
                a for a in accounts
                if self._category_for_platform(a.get("platform")) in wanted
            ]
        return self._render_account_lines(principal, accounts)

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

    def expected_email_for_account_id(self, account_id: str) -> str:
        """The email/handle an OAuth account_id is SUPPOSED to authenticate as (the
        registry account's resolved handle). Empty when no registry account claims
        this id. Used to validate the consented identity matches what we expect —
        see docs/architecture/SECRETS_ACCOUNTS.md."""
        aid = str(account_id or "").strip()
        if not aid:
            return ""
        for a in self.accounts():
            if str((a.get("auth") or {}).get("account_id") or "").strip() == aid:
                return str(a.get("handle") or "").strip()
        return ""

    # ── .env file overview (central settings page) ────────────────────────────
    # Per-var sensitivity (plain .env vars; accounts manage their own pods):
    #   config  — non-secret config/allowlist; masked in UI, no pod
    #   courier — secret only deterministic code reads; masked, no pod, never surfaced (100)
    #   agent   — secret an agent references via pod-ref; minted pod, surfaced (99)
    _SENSITIVITY = ("config", "courier", "agent")
    _DEFAULT_SENSITIVITY = "courier"  # fail-closed: secret, no pod, invisible

    def _entry_sensitivity(self, entry: Optional[dict[str, Any]]) -> str:
        s = str((entry or {}).get("sensitivity") or "").strip().lower()
        return s if s in self._SENSITIVITY else self._DEFAULT_SENSITIVITY

    def _account_env_vars(self) -> set[str]:
        """Env var names that back an account (handle / secret / pod-id). These are
        managed on the Accounts page, never reclassified as plain vars on the dev page."""
        names: set[str] = set()
        for e in self.entries():
            if e.get("kind") != "account":
                continue
            auth = e.get("auth") or {}
            for v in (e.get("handle_env"), auth.get("env_ref"), auth.get("pod_id_env")):
                v = str(v or "").strip()
                if v:
                    names.add(v)
        return names

    def _registry_meta_by_env_name(self) -> dict[str, dict[str, Any]]:
        """Map every env var NAME the registry knows about -> display metadata
        {label, hint, feature, owner, sensitivity, pod_id}. Covers value/secret
        entries (the name IS the env var) and account entries (their handle_env /
        auth.env_ref / auth.pod_id_env). Overlays labels + sensitivity on the real .env."""
        meta: dict[str, dict[str, Any]] = {}
        for e in self.entries():
            owner = str(e.get("owner") or "other")
            feature = str(e.get("feature") or "general")
            label = str(e.get("label") or e.get("name"))
            kind = e.get("kind")
            if kind == "account":
                auth = e.get("auth") or {}
                hv = str(e.get("handle_env") or "").strip()
                if hv:
                    meta[hv] = {"label": f"{label} — handle", "hint": "", "feature": feature,
                                "owner": owner, "sensitivity": "config", "pod_id": ""}
                er = str(auth.get("env_ref") or "").strip()
                if er:
                    meta[er] = {"label": f"{label} — {auth.get('secret_label') or 'secret'}",
                                "hint": str(auth.get("secret_hint") or ""), "feature": feature,
                                "owner": owner, "sensitivity": "agent",
                                "pod_id": self._resolve_auth_pod_id(e)}
                pe = str(auth.get("pod_id_env") or "").strip()
                if pe:
                    meta[pe] = {"label": f"{label} — auth pod id", "hint": "", "feature": feature,
                                "owner": owner, "sensitivity": "config", "pod_id": ""}
            else:  # value / secret / classification-only user entries
                meta[str(e["name"])] = {
                    "label": label, "hint": str(e.get("hint") or ""), "feature": feature,
                    "owner": owner, "sensitivity": self._entry_sensitivity(e),
                    "pod_id": str(e.get("pod_id") or ""),
                }
        return meta

    def env_overview(self) -> list[dict[str, Any]]:
        """The ACTUAL .env file as grouped, all-masked rows. The source of truth
        is the file, so a variable the user has not set never appears. Registry
        labels/features are overlaid where a name is known; unknown vars land
        under 'other'. Shape matches the central settings template:
        [{owner, features: [{feature, entries: [row]}]}]."""
        from dotenv import dotenv_values
        from app.assistant.utils.path_utils import get_env_file
        env_path = get_env_file()
        file_vars = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
        meta = self._registry_meta_by_env_name()
        account_vars = self._account_env_vars()
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
                "sensitivity": (m or {}).get("sensitivity") or self._DEFAULT_SENSITIVITY,
                "pod_id": (m or {}).get("pod_id") or "",
                "managed": name in account_vars,        # account-backed: knob suppressed
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

    # ── sensitivity classification + pod minting (dev page) ───────────────────
    def _mint_scope(self):
        """AUTH_USER (99) scope — the floor put_secret_pod requires to mint."""
        from app.assistant.utils.pydantic_classes import ScopeContext, ScopeApprovalPolicy
        from app.assistant.utils.identity_names import PRINCIPAL_USER
        return ScopeContext(
            scope_id="env-settings::mint",
            owner_id=PRINCIPAL_USER,
            actor_id="env-settings-ui",
            surface="internal",
            approval=ScopeApprovalPolicy(authority_level=99),
        )

    def _upsert_user_field(self, name: str, **fields: Any) -> dict[str, Any]:
        """Merge `fields` into the gitignored user-registry entry for `name`
        (create it if absent). Returns the merged entry."""
        data: dict[str, Any] = {"schema_version": 1, "entries": []}
        if _USER_PATH.exists():
            loaded = json.loads(_USER_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                data = loaded
        target = None
        for e in data["entries"]:
            if str(e.get("name")) == name:
                e.update(fields); target = e; break
        if target is None:
            target = {"name": name, **fields}
            data["entries"].append(target)
        _USER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    def ensure_pod_for(self, name: str, *, owner: str = "user") -> str:
        """Mint an auth.bearer pod for an agent-referenceable secret var if one
        doesn't exist yet, recording the pod_id in the user registry. The pod POINTS
        at the .env var (env_ref=name) — the value is read fresh at use, so re-setting
        the value needs no re-mint. Returns the pod_id."""
        entry = self.get(name) or {}
        existing = str(entry.get("pod_id") or "").strip()
        if existing:
            return existing
        from app.assistant.pod_store.pod_store import PodStore
        from app.assistant.utils.identity_names import resolve_principal
        pod_id = PodStore().put_secret_pod(
            pod_type="auth.bearer",
            owner_subject_id=resolve_principal(owner),
            name=str(entry.get("label") or name),
            env_ref=name,
            scope=self._mint_scope(),
        )
        self._upsert_user_field(name, pod_id=pod_id)
        return pod_id

    def _revoke_pod_for(self, name: str) -> str:
        """If a pod was minted for this var, hard-delete it and clear the link.
        Returns the revoked pod_id (or "")."""
        pod_id = str((self.get(name) or {}).get("pod_id") or "").strip()
        if not pod_id:
            return ""
        from app.assistant.pod_store.pod_store import PodStore
        PodStore().revoke_secret_pod(pod_id, scope=self._mint_scope())
        self._upsert_user_field(name, pod_id="")
        return pod_id

    def set_sensitivity(self, name: str, sensitivity: str, *, owner: str = "user") -> dict[str, Any]:
        """Classify a .env var (config | courier | agent). Promoting to 'agent' MINTS
        its pod (agent-referenceable); demoting from 'agent' HARD-DELETES the pod so it
        can no longer be dereferenced. Account-backing vars are managed on the Accounts
        page, not here."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("env registry: a variable name is required")
        s = str(sensitivity or "").strip().lower()
        if s not in self._SENSITIVITY:
            raise ValueError(f"env registry: sensitivity must be one of {self._SENSITIVITY}")
        entry = self.get(name)
        if (entry is not None and entry.get("kind") == "account") or name in self._account_env_vars():
            raise ValueError(f"env registry: {name!r} backs an account; manage it on the Accounts page")
        owner = str(owner or "user")
        result: dict[str, Any] = {"name": name, "sensitivity": s}
        if s == "agent":
            self._upsert_user_field(name, sensitivity=s, owner=owner)
            result["pod_id"] = self.ensure_pod_for(name, owner=owner)
        else:
            revoked = self._revoke_pod_for(name)   # fail-loud before relabeling
            self._upsert_user_field(name, sensitivity=s, owner=owner)
            if revoked:
                result["revoked"] = revoked
        return result

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

    # ── account creation (Accounts page) ──────────────────────────────────────
    _FEATURE_BY_PLATFORM = {
        "gmail": "email", "outlook": "email", "yahoo": "email", "icloud": "email",
        "bluesky": "social", "twitter": "social", "x": "social",
        "reddit": "social", "telegram": "social", "mastodon": "social",
    }

    @staticmethod
    def _slug(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_").upper()

    def create_account(self, *, owner: str, platform: str, login: str, secret: str,
                       authority: int = 99, label: str = "",
                       accessible_by: Optional[list[str]] = None) -> dict[str, Any]:
        """User-create an account in the UNIFIED registry (env_registry_user.json).

        Generates name-neutral `.env` keys (VALUES ONLY), writes them, mints the
        STANDARD auth.bearer pod (the mint is uniform — `authority` is NOT applied at
        mint; it's a surfacing policy stored on the entry), and persists the
        kind=account record. See docs/architecture/SECRETS_ACCOUNTS.md.
        Returns the projected account.
        """
        from app.assistant.utils.identity_names import resolve_principal
        owner = resolve_principal(owner) or "user"
        platform = str(platform or "").strip().lower()
        login = str(login or "").strip()
        secret = str(secret or "")
        if not platform:
            raise ValueError("platform is required")
        if not login:
            raise ValueError("login name is required")
        if not secret:
            raise ValueError("password / secret is required")
        authority = int(authority)
        if not (0 <= authority <= 100):
            raise ValueError("authority must be between 0 and 100")

        base = f"{self._slug(owner)}_{self._slug(platform)}_{self._slug(login)}".strip("_")
        handle_env = f"ACCT_{base}_HANDLE"
        secret_env = f"ACCT_{base}_SECRET"
        account_id = f"acct_{base.lower()}"
        if self.get(account_id) is not None:
            raise ValueError(f"account {account_id!r} already exists")

        # 1. .env holds VALUES only (key=value); structure goes to the registry below.
        from app.assistant.utils.env_writer import upsert_env
        upsert_env(handle_env, login); os.environ[handle_env] = login
        upsert_env(secret_env, secret); os.environ[secret_env] = secret

        # 2. UNIFORM mint — standard auth.bearer pod, no per-account authority in the mint.
        from app.assistant.pod_store.pod_store import PodStore
        pod_id = PodStore().put_secret_pod(
            pod_type="auth.bearer",
            owner_subject_id=owner,
            name=(label or f"{platform}:{login}"),
            env_ref=secret_env,
            scope=self._mint_scope(),
        )

        # 3. Structured record -> unified user registry (kind=account).
        record = {
            "kind": "account",
            "owner": owner,
            "platform": platform,
            "label": label or f"{platform}:{login}",
            "feature": self._FEATURE_BY_PLATFORM.get(platform, platform),
            "handle_env": handle_env,
            "accessible_by": accessible_by or [owner],   # sensible default; editable
            "authority": authority,                       # surfacing policy, not mint
            "auth": {"kind": "pod_ref", "pod_kind": "auth.bearer",
                     "env_ref": secret_env, "pod_id": pod_id},
        }
        self._upsert_user_field(account_id, **record)
        return self._project_account({"name": account_id, **record})
