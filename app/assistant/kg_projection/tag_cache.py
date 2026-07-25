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
from typing import Dict, List, Optional

from app.assistant.utils.filename_safety import safe_filename
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def bullet_key(bullet_text: str) -> str:
    """Stable short hash for cache keying. Same input → same key across runs."""
    return hashlib.sha256(bullet_text.encode("utf-8")).hexdigest()[:16]


def load_tags(root: Path, entity_label: str) -> Dict[str, List[str]]:
    """Read the tag sidecar for an entity. Missing file → empty dict."""
    path = root / "tags" / f"{safe_filename(entity_label)}.json"
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
    path = out_dir / f"{safe_filename(entity_label)}.json"
    path.write_text(json.dumps(tags, indent=2, ensure_ascii=False), encoding="utf-8")


def load_bullet_index(root: Path, entity_label: str) -> Dict[str, Optional[str]]:
    """Read the bullet-index sidecar — ``{bullet_key: bullet_text}`` for the
    bullets present at the last successful rewrite. Used by incremental
    refresh to detect which bullets were added/removed since the prose was
    last written; the stored text is what lets a removal be named in the
    section-revision prompt ("delete the claim that X").

    Distinct from the tags sidecar (which is the tagger cache, written
    immediately after tagging): bullet_index is the rewrite checkpoint,
    written only after the prose actually lands. A crash mid-rewrite leaves
    the old index in place so the next run re-detects everything as dirty
    and self-heals.

    Legacy sidecars (a bare list of keys, written before 2026-07-25) load as
    ``{key: None}`` — the key set still diffs correctly, but removed-bullet
    text is unknown until the next save rewrites the file in mapping form.

    Missing file → empty dict (first-run; everything will appear "added").
    """
    path = root / "bullet_index" / f"{safe_filename(entity_label)}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(k): None for k in data if isinstance(k, str)}
        if isinstance(data, dict):
            return {
                str(k): (v if isinstance(v, str) else None)
                for k, v in data.items()
            }
    except Exception as e:
        logger.warning("Failed reading bullet_index sidecar %s: %s", path, e)
    return {}


def save_bullet_index(root: Path, entity_label: str, bullets: Dict[str, str]) -> None:
    """Write the bullet-index sidecar as ``{bullet_key: bullet_text}``
    (key-sorted for diffability)."""
    out_dir = root / "bullet_index"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_filename(entity_label)}.json"
    path.write_text(
        json.dumps(dict(sorted(bullets.items())), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
