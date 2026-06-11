"""Subsystem feature flags — read from configs/subsystems.yaml.

Usage:
    from app.assistant.utils.subsystem_flags import is_subsystem_enabled
    if is_subsystem_enabled("dayflow_scheduler"):
        scheduler.start()
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir

logger = get_logger(__name__)


def _config_path() -> Path:
    """subsystems.yaml under the configs dir. Computed lazily — not at
    import — so a launcher that sets EMI_DATA_DIR/EMI_CONFIGS_DIR before
    create_app is respected."""
    return get_configs_dir() / "subsystems.yaml"


@lru_cache(maxsize=1)
def _load_config() -> dict:
    if not _config_path().exists():
        return {}
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("Loaded subsystem flags from %s: %s", _config_path(), data)
        return data
    except Exception as e:
        logger.error("Failed to load subsystem flags: %s", e)
        return {}


def _reload_config():
    """Clear the cache and reload from disk."""
    _load_config.cache_clear()
    return _load_config()


def is_subsystem_enabled(name: str) -> bool:
    """Check if a subsystem is enabled. Defaults to True if not configured."""
    config = _load_config()
    value = config.get(name)
    if value is None:
        return True  # Default: enabled
    return bool(value)


def get_all_flags() -> dict:
    """Return all subsystem flags (current state from disk)."""
    return dict(_reload_config())


def set_subsystem_flag(name: str, enabled: bool) -> dict:
    """Set a single subsystem flag and persist to disk. Returns updated flags."""
    config = _reload_config()
    config[name] = enabled
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False)
        logger.info("Subsystem flag updated: %s = %s", name, enabled)
    except Exception as e:
        logger.error("Failed to save subsystem flags: %s", e)
        raise
    # Clear cache so next is_subsystem_enabled() reads fresh.
    _load_config.cache_clear()
    return config
