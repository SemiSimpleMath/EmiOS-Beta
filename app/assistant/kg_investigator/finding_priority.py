"""Importance-aware ordering for the kg_maintenance_finding queues.

The drain queries (executor + processor) ask: "of the eligible findings,
which N should I work on now?" FIFO-by-date treats Jukka and Katy's
wedding the same as Seija buying a backpack. Importance ordering says:
work on the wedding first, the backpack last.

The score: kg_node_metadata.importance of the primary node when set
(the rater already derives this from edge importance — a State between
Jukka and Katy with `is_married` edges weighted 10.0 lands at ~0.95;
a State touching only minor things lands much lower). When the rater
hasn't run on a node yet (importance=None), fall back to
MAX(edge.importance) across the edges connecting to it — the same
signal, computed on the fly.

This is a runtime sort, not a stored column — no schema change.
~1ms per finding even with the join, called only at drain time on the
small candidate set.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import text as sql_text

from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


def importance_score(primary_node_id: str) -> float:
    """Return the primary node's importance score for ranking findings.

    Primary signal: kg_node_metadata.importance, which the rater derives
    from edge importance. A State between two highly-connected entities
    (e.g. Jukka and Katy via is_married edge_imp=10.0) scores ~0.95;
    a Seija-likes-hiking State scores ~0.66.

    Fallback when importance is NULL (rater hasn't visited this node):
    max(edge.importance)/10 across edges connecting to it — same signal
    as the rater's input, divided by 10 so it lands on the same 0-1
    scale as stored state.importance for fair comparison.

    Returns 0.0 only when the node has no importance and no edges.
    """
    if not primary_node_id:
        return 0.0
    try:
        with get_db_manager().read_session() as session:
            row = session.execute(
                sql_text(
                    "SELECT importance FROM kg_node_metadata WHERE id = :nid"
                ),
                {"nid": primary_node_id},
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])

            # Fallback: max edge importance touching this node, scaled
            # to 0-1 to match state.importance.
            row = session.execute(
                sql_text(
                    """
                    SELECT MAX(importance)
                    FROM kg_edge_metadata
                    WHERE source_id = :nid OR target_id = :nid
                    """
                ),
                {"nid": primary_node_id},
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0]) / 10.0
        return 0.0
    except Exception as e:
        logger.warning("[finding_priority] importance lookup failed for %s: %s", primary_node_id, e)
        return 0.0


def order_by_importance(
    candidate_ids_with_dates: Iterable[Tuple[str, str, object]],
) -> List[str]:
    """Sort (finding_id, primary_node_id, date_anchor) tuples by
    importance DESC, with the date_anchor as tie-breaker (oldest first).
    Returns a flat list of finding_ids in the new order.
    """
    scored = []
    for fid, pnid, date_anchor in candidate_ids_with_dates:
        scored.append((importance_score(pnid), date_anchor, fid))
    # Sort: importance DESC, then date ASC (oldest first within a tier).
    scored.sort(key=lambda x: (-x[0], x[1] or 0))
    return [fid for _, _, fid in scored]
