from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_repo_root() -> Path:
    """
    Canonical repository root.

    Keep all runtime code on this helper rather than Path(__file__).parents[N]
    to avoid brittle path regressions when files move.
    """
    env_root = os.getenv("EMI_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def get_app_root() -> Path:
    env_app_root = os.getenv("EMI_APP_ROOT")
    if env_app_root:
        p = Path(env_app_root).expanduser()
        return p.resolve() if p.is_absolute() else (get_repo_root() / p).resolve()
    return get_repo_root() / "app"


@lru_cache(maxsize=1)
def get_data_dir() -> Path:
    """Root for ALL user-writable runtime state (db, .env, chroma, resources,
    configs, logs, uploads, ...).

    The packaged launcher sets ``EMI_DATA_DIR`` to a per-user location
    (e.g. ``%LOCALAPPDATA%\\EmiOS\\data``) that updates never touch. In dev it
    defaults to the repo root, so when ``EMI_DATA_DIR`` is unset every derived path
    is byte-for-byte the legacy location.
    """
    env_data = os.getenv("EMI_DATA_DIR")
    if env_data:
        return Path(env_data).expanduser().resolve()
    return get_repo_root()


@lru_cache(maxsize=1)
def get_env_file() -> Path:
    """Path to the ``.env`` file (secrets, ``key=value``). Writable, so it lives
    under the data dir: ``EMI_ENV_FILE`` wins; else ``get_data_dir()/.env`` (which
    is repo-root/.env in dev, so unchanged when ``EMI_DATA_DIR`` is unset)."""
    env_file = os.getenv("EMI_ENV_FILE")
    if env_file:
        return Path(env_file).expanduser().resolve()
    return get_data_dir() / ".env"


@lru_cache(maxsize=1)
def get_uploads_dir() -> Path:
    """Writable scratch root for uploads + transient tool artifacts (screenshots,
    tool-result blobs, slack image downloads).

    Under the data dir: ``EMI_UPLOADS_DIR`` wins; else ``get_data_dir()/uploads``
    (which is repo-root/uploads in dev). Callers usually append ``/'temp'/...``.
    """
    env_uploads = os.getenv("EMI_UPLOADS_DIR")
    if env_uploads:
        return Path(env_uploads).expanduser().resolve()
    return get_data_dir() / "uploads"


@lru_cache(maxsize=1)
def get_seed_resources_dir() -> Path:
    """Code-layer (read-only, bundled) source of first-run resource SEED templates
    (the ``.example`` files).

    Always the code-root ``resources/`` — NEVER the writable data dir — so the
    packaged app reads templates from the app layer and writes the live files under
    ``get_data_dir()``. In dev this equals ``get_resources_dir()`` (both repo-root),
    so seeding behaves exactly as before.
    """
    return get_repo_root() / "resources"


def _resolve_from_app_root(path_value: str) -> Path:
    p = Path(path_value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (get_app_root() / p).resolve()


def resolve_repo_path(path_value: str | Path) -> Path:
    """
    Resolve an absolute/relative path against repository root.
    """
    p = Path(path_value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (get_repo_root() / p).resolve()


def as_repo_relative(path_value: str | Path) -> str:
    """
    Return POSIX repo-relative path; fail loudly when outside repo.
    """
    resolved = resolve_repo_path(path_value)
    return resolved.relative_to(get_repo_root()).as_posix()


def is_within_repo(path_value: str | Path) -> bool:
    try:
        resolve_repo_path(path_value).relative_to(get_repo_root())
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_resources_dir() -> Path:
    env_resources = os.getenv("EMI_RESOURCES_DIR")
    if env_resources:
        return Path(env_resources).expanduser().resolve()
    if os.getenv("EMI_DATA_DIR"):
        return get_data_dir() / "resources"
    return get_repo_root() / "resources"


@lru_cache(maxsize=1)
def get_configs_dir() -> Path:
    """
    Runtime configuration directory.

    This is intentionally separate from `resources/`, which is reserved for
    prompt-injectable, user-editable resource artifacts.
    """
    env_cfg = os.getenv("EMI_CONFIGS_DIR")
    if env_cfg:
        return Path(env_cfg).expanduser().resolve()
    if os.getenv("EMI_DATA_DIR"):
        return get_data_dir() / "configs"
    return get_repo_root() / "configs"


@lru_cache(maxsize=1)
def get_chroma_kg_db_dir() -> Path:
    """
    Directory for KG Chroma persistent storage.

    Uses EMI_CHROMA_KG_DB_DIR when provided; otherwise defaults to
    <app_root>/chroma_db.

    If EMI_CHROMA_KG_DB_DIR is a relative path, it is resolved from app root.
    """
    env_chroma = os.getenv("EMI_CHROMA_KG_DB_DIR")
    if env_chroma:
        return _resolve_from_app_root(env_chroma)
    if os.getenv("EMI_DATA_DIR"):
        return get_data_dir() / "chroma_db"
    return get_app_root() / "chroma_db"


def setup_complete() -> bool:
    """Return True if the setup wizard (or a legacy manual setup) has been completed.

    Checks in order:
    1. SETUP_COMPLETE env var (written by the new wizard)
    2. Existence of resource_user_data.json (wizard output)
    3. Existence of the SQLite database file (legacy setup indicator)
    """
    if os.environ.get("SETUP_COMPLETE", "").strip().lower() in {"true", "1", "yes"}:
        return True

    setup_flag = get_repo_root() / ".setup_complete"
    if setup_flag.exists():
        return True

    return False
