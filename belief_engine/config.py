"""Loader for the belief-engine domain config.

Reads `configs/belief_domains.yaml` and exposes domain metadata to the
pipeline steps. Single source of truth — replaces the formerly-hardcoded
`_DOMAIN_TAGS` and `_DOMAIN_TICKET_TYPES` dicts that drifted relative to
the registered pipelines and routines.

Loaded once per process (cached). Calling `reload_domains()` clears the
cache for tests / hot-edits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir

logger = get_logger(__name__)

_CONFIG_FILENAME = "belief_domains.yaml"
_cache: Optional[Dict[str, "DomainConfig"]] = None


@dataclass(frozen=True)
class DomainConfig:
    id: str
    enabled: bool
    tags: List[str] = field(default_factory=list)
    ticket_types: List[str] = field(default_factory=list)


def _config_path() -> Path:
    return get_configs_dir() / _CONFIG_FILENAME


def _load() -> Dict[str, DomainConfig]:
    path = _config_path()
    if not path.exists():
        logger.warning("[belief_engine.config] %s not found — no domains will run.", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: Dict[str, DomainConfig] = {}
    for entry in raw.get("domains") or []:
        if not isinstance(entry, dict):
            continue
        did = str(entry.get("id") or "").strip()
        if not did:
            continue
        if did in out:
            logger.warning("[belief_engine.config] duplicate domain id %r — last write wins.", did)
        out[did] = DomainConfig(
            id=did,
            enabled=bool(entry.get("enabled", False)),
            tags=[str(t) for t in (entry.get("tags") or [])],
            ticket_types=[str(t) for t in (entry.get("ticket_types") or [])],
        )
    return out


def _domains() -> Dict[str, DomainConfig]:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reload_domains() -> None:
    """Drop the cache so the next call re-reads the YAML."""
    global _cache
    _cache = None


def get_domain_config(domain_id: str) -> Optional[DomainConfig]:
    return _domains().get(domain_id)


def list_enabled_domains() -> List[DomainConfig]:
    """Domains with `enabled: true`, in YAML declaration order."""
    return [d for d in _domains().values() if d.enabled]


def list_all_domain_ids() -> List[str]:
    return list(_domains().keys())


def list_all_tags(*, only_enabled: bool = True) -> List[str]:
    """Union of every domain's tags, deduped, sorted.

    This is the canonical tag vocabulary upstream producers (e.g., the
    daily_timeline_insights LLM agent) should be told they may emit.
    Adding a tag to any domain in the YAML is the only edit needed —
    upstream picks it up on next run.
    """
    seen: set[str] = set()
    out: List[str] = []
    for d in _domains().values():
        if only_enabled and not d.enabled:
            continue
        for t in d.tags:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return sorted(out)
