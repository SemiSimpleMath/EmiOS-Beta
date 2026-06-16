"""Standardized belief TAG vocabulary (configs/belief_tags.yaml) + belief_tags I/O.

The vocab is the CONTROLLED set every tag writer is validated against (the
anti-proliferation guarantee). `domain` (belief_category) stays the nightly
derivation lane; tags are the ADDITIVE retrieval layer — consumers pull by a tag
SET (`pull_set`), and a belief surfaces if it carries ANY tag in that set. Bridge
tags (dietary, family, social, meal) reach beliefs filed under another domain.
"""
from __future__ import annotations

import functools
from datetime import datetime, timezone
from typing import Iterable, List, Set

import yaml

from app.assistant.utils.path_utils import get_repo_root

_CONFIG = get_repo_root() / "configs" / "belief_tags.yaml"


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    with open(_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg.get("tags"), dict) or not cfg["tags"]:
        raise ValueError(f"belief_tags.yaml has no 'tags' map: {_CONFIG}")
    return cfg


def vocab() -> dict:
    """tag -> one-line definition (the standard set)."""
    return dict(_load()["tags"])


def valid_tags() -> Set[str]:
    return set(_load()["tags"].keys())


def pull_set(name: str) -> List[str]:
    """The tag list a named consumer retrieves (e.g. 'meal_engine'), validated against vocab."""
    tags = (_load().get("pull_sets") or {}).get(name)
    if not tags:
        raise KeyError(f"no pull_set '{name}' in belief_tags.yaml")
    bad = [t for t in tags if t not in valid_tags()]
    if bad:
        raise ValueError(f"pull_set '{name}' has off-vocab tags: {bad}")
    return list(tags)


def sanitize(tags: Iterable[str]) -> List[str]:
    """Keep only in-vocab tags (dedup, lowercased) — the enforcement point for any writer."""
    v = valid_tags()
    seen: Set[str] = set()
    out: List[str] = []
    for t in tags or []:
        t = str(t or "").strip().lower()
        if t in v and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── belief_tags I/O — conn is the belief store connection (same db as beliefs) ──
def set_tags(conn, belief_id: str, tags: Iterable[str], *, method: str = "categorizer") -> List[str]:
    """Replace a belief's tags with the sanitized set; returns what was written."""
    clean = sanitize(tags)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM belief_tags WHERE belief_id=?", (belief_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO belief_tags (belief_id, tag, assigned_at, method) VALUES (?,?,?,?)",
        [(belief_id, t, now, method) for t in clean],
    )
    conn.commit()
    return clean


def get_tags(conn, belief_id: str) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT tag FROM belief_tags WHERE belief_id=? ORDER BY tag", (belief_id,))]


def belief_ids_with_any(conn, tags: Iterable[str]) -> Set[str]:
    """Belief ids carrying ANY of the given (sanitized) tags — the pull-set scope."""
    clean = sanitize(tags)
    if not clean:
        return set()
    ph = ",".join("?" for _ in clean)
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT belief_id FROM belief_tags WHERE tag IN ({ph})", clean)}
