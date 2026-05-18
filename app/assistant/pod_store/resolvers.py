"""Value resolvers for pod projections.

A `PodProjection` row holds a POINTER, not the value itself, for
authority-banded projections. This module turns the pointer into the
actual value at fetch time.

Three resolver paths matching the three `storage_kind` values:

  'plain'  → return `plain_value` from the DB row directly. No
             indirection. Used for low-authority derived projections
             that aren't sensitive enough to warrant pointer storage
             (last4, redacted, format).

  'env'    → return `os.environ[env_ref]`. Used for string-shaped
             secrets the user types into .env. The codebase never
             holds the secret literally — it holds the env var name.

  'file'   → return bytes from `data/pod_secrets/<file_ref>`. Used
             for binary artifact pods (filled forms, scanned IDs).
             Directory perm-restricted to user (chmod 700 on POSIX,
             ACL on Windows).

All resolvers raise `PodValueMissing` if the underlying source is
absent (env var unset, file deleted). The PodStore layer catches
and surfaces this as a structural error — the pod's pointer is
stale, and the operation aborts rather than substitute a default.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union


class PodValueMissing(Exception):
    """The pod's projection points at something that isn't there.

    Env var unset, file deleted, etc. Caller should treat as a
    structural error (the pod's pointer is stale) rather than
    a permission denial.
    """
    pass


# Per-machine root for file-backed projections (artifact pods).
# Created on demand at first use; should be chmod 700 by setup.
_SECRETS_DIR_NAME = "pod_secrets"


def _secrets_dir() -> Path:
    from app.assistant.utils.path_utils import get_repo_root
    d = get_repo_root() / "data" / _SECRETS_DIR_NAME
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            # POSIX-only — Windows ignores; that's fine for the dev
            # local-first setup, the dir lives in the user's profile.
            os.chmod(d, 0o700)
        except (OSError, PermissionError):
            pass
    return d


def resolve_plain(plain_value: str) -> str:
    """Plain-storage resolver. Identity function — value lives in the
    row, no indirection. Provided for API consistency."""
    if plain_value is None:
        raise PodValueMissing("plain_value is NULL on a plain-storage row")
    return plain_value


def resolve_env(env_ref: str) -> str:
    """Env-storage resolver. Reads `os.environ[env_ref]` and returns
    the value. Raises `PodValueMissing` if unset or empty.
    """
    if not env_ref:
        raise PodValueMissing("env_ref is empty on an env-storage row")
    value = os.environ.get(env_ref)
    if value is None or value == "":
        raise PodValueMissing(
            f"env var {env_ref!r} is unset or empty — "
            f"add it to .env and restart"
        )
    return value


def resolve_file(file_ref: str) -> bytes:
    """File-storage resolver. Reads bytes from
    `data/pod_secrets/<file_ref>`. Raises `PodValueMissing` if
    the file is missing.
    """
    if not file_ref:
        raise PodValueMissing("file_ref is empty on a file-storage row")
    path = _secrets_dir() / file_ref
    if not path.exists():
        raise PodValueMissing(
            f"file {path} is missing — pod projection is dangling"
        )
    return path.read_bytes()


def write_file(file_ref: str, data: Union[bytes, str]) -> None:
    """Write bytes/string to `data/pod_secrets/<file_ref>`. Used by the
    courier when creating artifact pods. Creates the secrets dir if
    needed."""
    if not file_ref:
        raise ValueError("file_ref is required")
    path = _secrets_dir() / file_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except (OSError, PermissionError):
        pass
