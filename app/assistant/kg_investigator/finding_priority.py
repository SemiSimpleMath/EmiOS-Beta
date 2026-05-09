"""Importance-aware ordering for the kg_maintenance_finding queues.

The drain queries (executor + processor) ask: "of the eligible findings,
which N should I work on now?" FIFO-by-date treats Jukka and Katy's
wedding the same as Seija buying a backpack. Importance ordering says:
work on the wedding first, the backpack last.

The score: max kg_node_metadata.importance across the finding's primary
node and its one-hop neighbors. That way a State node (low-importance
on its own scale) connecting two high-importance Entities inherits the
right priority, and a State touching only minor entities/concepts ranks
low.

This is a runtime sort, not a stored column — no schema change. ~1ms
per finding even with the join, called only at drain time on the small
candidate set.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import text as sql_text

from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


def importance_score(primary_node_id: str) -> float:
    """Return the max importance across the primary node + its one-hop
    neighbors. Returns 0.0 when the node is missing or has no
    importance set.

    A finding is "important" if it touches important things — a State
    between Jukka and Katy is important because Jukka and Katy are
    important entities, even though the State node itself sits on a
    different importance scale.
    """
    if not primary_node_id:
        return 0.0
    try:
        with get_db_manager().read_session() as session:
            row = session.execute(
                sql_text(
                    """
                    SELECT MAX(imp) FROM (
                      SELECT importance AS imp
                      FROM kg_node_metadata
                      WHERE id = :nid
                      UNION ALL
                      SELECT n.importance
                      FROM kg_edge_metadata e
                      JOIN kg_node_metadata n
                        ON n.id = (CASE WHEN e.source_id = :nid
                                        THEN e.target_id ELSE e.source_id END)
                      WHERE e.source_id = :nid OR e.target_id = :nid
                    )
                    """
                ),
                {"nid": primary_node_id},
            ).fetchone()
        if not row or row[0] is None:
            return 0.0
        return float(row[0])
    except Exception as e:
        logger.warning("[finding_priority] importance lookup failed for %s: %s", primary_node_id, e)
        return 0.0


def order_by_importance(
    candidate_ids_with_dates: Iterable[Tuple[str, str, object]],
) -> List[str]:
    """Sort (finding_id, primary_node_id, date_anchor) tuples by max
    importance DESC, with the date_anchor as tie-breaker (oldest first).
    Returns a flat list of finding_ids in the new order.
    """
    scored = []
    for fid, pnid, date_anchor in candidate_ids_with_dates:
        scored.append((importance_score(pnid), date_anchor, fid))
    # Sort: importance DESC, then date ASC (oldest first within an
    # importance tier). Negate importance for ascending sort.
    scored.sort(key=lambda x: (-x[0], x[1] or 0))
    return [fid for _, _, fid in scored]
