"""Unified env-var registry — read service.

Loads BUILTIN entries (shipped, tracked: builtins.json) merged with USER entries
(gitignored, UI-written: resources/env_registry_user.json) and exposes lookups for
the central settings UI, inline feature-page sections, account resolution, and env
var-name indirection.

Metadata only: actual VALUES are never stored here — plain values live in .env
(kind=value), secrets in pods (kind=secret / account.auth). This holds the env var
NAMES + display metadata so code stops hardcoding names like EMI_OWN_GMAIL_HANDLE.

No guessed defaults: a missing entry or field raises (the caller decides), per the
project's fail-loud rule.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.assistant.utils.identity_names import resolve_principal
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root, get_resources_dir

logger = get_logger(__name__)

_BUILTINS_PATH = get_app_root() / "assistant" / "env_registry" / "builtins.json"
_USER_PATH = get_resources_dir() / "env_registry_user.json"


class EnvRegistryService:
    """Read-only view over builtin + user env-var registry entries."""

    def _load(self, path: Path, *, required: bool) -> list[dict[str, Any]]:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"env registry: required file missing: {path}")
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError(f"env registry {path.name}: top-level 'entries' list is required")
        out: list[dict[str, Any]] = []
        for e in data["entries"]:
            if isinstance(e, dict) and str(e.get("name") or "").strip():
                out.append(e)
        return out

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

    def accounts_for_principal(self, principal: str) -> list[dict[str, Any]]:
        """kind==account entries whose accessible_by includes the (canonicalized) principal."""
        target = resolve_principal(principal)
        out: list[dict[str, Any]] = []
        for e in self.entries():
            if e.get("kind") != "account":
                continue
            allowed = e.get("accessible_by") or []
            if any(resolve_principal(str(p)) == target for p in allowed if isinstance(p, str)):
                out.append(e)
        return out

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
