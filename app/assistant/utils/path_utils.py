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
