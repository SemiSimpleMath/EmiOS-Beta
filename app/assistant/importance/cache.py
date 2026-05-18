"""In-memory cache for node importance scores.

Loads `kg_node_metadata.importance` once per process and serves repeat
reads from RAM. The lens/me consumers (api.py, layout.py, pagerank.py)
read this in tight loops; a per-call DB hit per node would dominate
their cost.

## History

Pre-2026-05-17 this cache loaded from `data/me_node_importance.json` —
a separate file the deprecated LLM Entity rater used to write to.
After that rater was removed, the JSON went stale (nothing wrote to
it anymore) but the cache kept reading it, so lens consumers were
working off frozen importance values.

Fixed: now loads directly from the DB column. The JSON file is
vestigial — `get_importance_map()` no longer touches it. The file
itself can be deleted in a follow-up cleanup; leaving it on disk for
now in case any external tooling reads it.

## Refresh discipline

Cache is loaded once, lazily, on first access. After the scoring
layer rewrites `kg_node_metadata.importance` (via
`regenerate_entity_importance` / `regenerate_state_importance` in
the maintenance routine), call `invalidate()` to force a reload on
the next access.

Concurrency: this is a single-process Python cache. Multi-worker
deployments would need per-worker invalidation or a different
backing store; for now (single Flask process) the simple approach is
fine.
"""
from __future__ import annotations

from typing import Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Default score returned when a node id is missing from the cache.
# Mid-scale — neither "important" nor "trivia."
DEFAULT_SCORE: float = 5.0

_CACHE: Optional[Dict[str, float]] = None


def get_importance_map() -> Dict[str, float]:
    """Return {node_id: importance} for every rated node.

    Lazy-loaded from `kg_node_metadata.importance` on first call.
    Subsequent calls return the cached map until `invalidate()` is
    called.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.db_manager import get_db_manager
        with get_db_manager().read_session() as session:
            rows = (
                session.query(Node.id, Node.importance)
                .filter(Node.importance.isnot(None))
                .all()
            )
            _CACHE = {str(nid): float(imp) for nid, imp in rows if imp is not None}
        logger.info(
            "importance cache: loaded %d scores from kg_node_metadata.importance",
            len(_CACHE),
        )
        return _CACHE
    except Exception as e:
        logger.warning("importance cache: DB load failed: %s — using empty map", e)
        _CACHE = {}
        return _CACHE


def get_importance(node_id: str) -> float:
    """Return importance score for a node id, or DEFAULT_SCORE if unrated."""
    return get_importance_map().get(node_id, DEFAULT_SCORE)


def invalidate() -> None:
    """Drop the in-memory cache. Next get_importance_map() reloads from DB.

    Call after the scoring layer rewrites `kg_node_metadata.importance`.
    """
    global _CACHE
    _CACHE = None
