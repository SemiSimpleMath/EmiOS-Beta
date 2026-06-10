"""Importance-aware ordering for the kg_maintenance_finding queues.

The drain queries (executor + processor) ask: "of the eligible findings,
which N should I work on now?" FIFO-by-date treats Jukka and Katy's
wedding the same as Seija buying a backpack. Importance ordering says:
work on the wedding first, the backpack last. (The claim functions then
reserve one slot for the oldest pending row so low-importance findings
still drain eventually — audit P3.1.)

The score: kg_node_metadata.importance of the primary node when set
(the rater already derives this from edge importance — a State between
Jukka and Katy with `is_married` edges weighted 10.0 lands at ~0.95;
a State touching only minor things lands much lower). When the rater
hasn't run on a node yet (importance=None), the score is
MAX(edge.importance)/10 across the edges connecting to it — the same
signal the rater consumes, computed on the fly.

This is a runtime sort, not a stored column — no schema change. All
lookups for one drain tick are batched into two queries (audit P3.1
flagged the old per-row session-per-finding N+1).
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import bindparam
from sqlalchemy import text as sql_text

from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


def _batch_importance_scores(node_ids: List[str]) -> dict[str, float]:
    """Two queries for the whole candidate set: stored node importance for
    everyone, then MAX(edge.importance)/10 for nodes the rater hasn't
    visited. A node missing from both contributes nothing (caller's .get
    default scores it 0.0 — no importance signal exists for it)."""
    scores: dict[str, float] = {}
    if not node_ids:
        return scores
    try:
        with get_db_manager().read_session() as session:
            rows = session.execute(
                sql_text(
                    "SELECT id, importance FROM kg_node_metadata "
                    "WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": node_ids},
            ).fetchall()
            for nid, imp in rows:
                if imp is not None:
                    scores[str(nid)] = float(imp)

            missing = [nid for nid in node_ids if nid not in scores]
            if missing:
                rows = session.execute(
                    sql_text(
                        "SELECT nid, MAX(imp) FROM ("
                        "  SELECT source_id AS nid, importance AS imp "
                        "  FROM kg_edge_metadata WHERE source_id IN :ids "
                        "  UNION ALL "
                        "  SELECT target_id AS nid, importance AS imp "
                        "  FROM kg_edge_metadata WHERE target_id IN :ids"
                        ") GROUP BY nid"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": missing},
                ).fetchall()
                for nid, mx in rows:
                    if mx is not None:
                        # Edge importance is 0-10; scale to the 0-1 range of
                        # stored node importance for fair comparison.
                        scores[str(nid)] = float(mx) / 10.0
    except Exception as e:
        logger.warning("[finding_priority] batch importance lookup failed: %s", e)
    return scores


def order_by_importance(
    candidate_ids_with_dates: Iterable[Tuple[str, str, object]],
) -> List[str]:
    """Sort (finding_id, primary_node_id, date_anchor) tuples by
    importance DESC, with the date_anchor as tie-breaker (oldest first).
    Returns a flat list of finding_ids in the new order.
    """
    candidates = list(candidate_ids_with_dates)
    scores = _batch_importance_scores(
        list({pnid for _, pnid, _ in candidates if pnid})
    )
    scored = [
        (scores.get(pnid, 0.0), date_anchor, fid)
        for fid, pnid, date_anchor in candidates
    ]
    # Sort: importance DESC, then date ASC (oldest first within a tier).
    scored.sort(key=lambda x: (-x[0], x[1] or 0))
    return [fid for _, _, fid in scored]
