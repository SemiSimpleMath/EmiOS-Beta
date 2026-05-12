"""Pod kinds registry — declarative metadata for every pod kind Emi knows about.

Loads ``configs/pod_kinds.json`` once at startup and exposes lookups
that replace the hardcoded sets that used to live inside ``kg_mirror``.

Adding a new pod kind is now a config edit:

    {
      "kinds": {
        "audio_clip": {
          "description": "...",
          "kg_admissible": true,
          "body_extraction": "transcription",
          "default_for_agents": []
        }
      }
    }

See ``skills/extending-emi-pod-kinds/SKILL.md`` for the field reference.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_REGISTRY_PATH = Path(get_repo_root()) / "configs" / "pod_kinds.json"

_cache: Optional[Dict[str, Any]] = None


def _load(*, force_reload: bool = False) -> Dict[str, Any]:
    """Lazy-load configs/pod_kinds.json. Missing file = empty registry
    (every kind defaults to NOT auto-mirroring — fail closed)."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    if not _REGISTRY_PATH.exists():
        logger.warning("[pod_kind_registry] %s not found; using empty registry", _REGISTRY_PATH)
        _cache = {"kinds": {}, "chat_introduced_source_kinds": []}
        return _cache
    raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    kinds = raw.get("kinds")
    if not isinstance(kinds, dict):
        raise ValueError(f"pod_kinds.json: 'kinds' must be a mapping (got {type(kinds).__name__})")
    chat_intro = raw.get("chat_introduced_source_kinds") or []
    if not isinstance(chat_intro, list):
        raise ValueError("pod_kinds.json: 'chat_introduced_source_kinds' must be a list")
    _cache = {
        "kinds": kinds,
        "chat_introduced_source_kinds": [str(s).strip() for s in chat_intro if isinstance(s, str)],
    }
    logger.info(
        "[pod_kind_registry] loaded %d kind(s): %s",
        len(kinds), ", ".join(sorted(kinds.keys())),
    )
    return _cache


def known_kinds() -> List[str]:
    return sorted(_load()["kinds"].keys())


def get_kind(kind: str) -> Optional[Dict[str, Any]]:
    """Return the entry for one kind, or None if unregistered."""
    return _load()["kinds"].get(str(kind or "").strip()) or None


def is_kg_admissible(kind: str) -> bool:
    """True if pods of this kind can appear as endpoints of KG edges.

    The promoter checks this before writing edges with `datapod:*` endpoints.
    Pods are content storage in pod_store; this flag controls whether they
    can also be REFERENCED by the KG. Source of truth is always pod_store —
    nothing is mirrored.

    Unknown kinds default to False (fail closed)."""
    entry = get_kind(kind)
    if not entry:
        return False
    return bool(entry.get("kg_admissible", False))


def chat_introduced_source_kinds() -> FrozenSet[str]:
    """source_kind values that ALWAYS admit to the KG regardless of pod kind."""
    return frozenset(_load()["chat_introduced_source_kinds"])


def description(kind: str) -> Optional[str]:
    entry = get_kind(kind)
    return str(entry.get("description")) if entry and entry.get("description") else None
