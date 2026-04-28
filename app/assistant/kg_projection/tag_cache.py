"""Tagger cache — per-bullet tag sidecar + content-hash key.

The tagger is expensive (one LLM batch call per ~20 new bullets). Almost
all bullets are stable between runs — only a handful change when the KG
gets new edges. Caching per-bullet by content hash lets re-runs pay only
for the bullets whose text actually changed.

On-disk JSON sidecars, written under ``<root>/tags/<entity_label>.json``.
The ``root`` path is caller-controlled so wiki can keep using its vault
(``<vault>/tags/``) and cards can point at a neutral location
(``data/kg_projection/tags/``).

Format:
    {"<bullet_key>": ["<section_key>", ...], ...}
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def bullet_key(bullet_text: str) -> str:
    """Stable short hash for cache keying. Same input → same key across runs."""
    return hashlib.sha256(bullet_text.encode("utf-8")).hexdigest()[:16]


def load_tags(root: Path, entity_label: str) -> Dict[str, List[str]]:
    """Read the tag sidecar for an entity. Missing file → empty dict."""
    path = root / "tags" / f"{entity_label}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except Exception as e:
        logger.warning("Failed reading tag sidecar %s: %s", path, e)
    return {}


def save_tags(root: Path, entity_label: str, tags: Dict[str, List[str]]) -> None:
    """Write the tag sidecar for an entity. Creates ``tags/`` if needed."""
    out_dir = root / "tags"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{entity_label}.json"
    path.write_text(json.dumps(tags, indent=2, ensure_ascii=False), encoding="utf-8")
