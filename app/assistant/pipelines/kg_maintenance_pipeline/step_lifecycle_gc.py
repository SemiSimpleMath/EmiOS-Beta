"""Lifecycle GC — sweep deletion residue the write paths can't reach.

The evidence tables have no FKs (nothing cascades) and duplicate-scan
verdicts are superseded only at merge time, so any historical deletion
that predates the 2026-07-08 lifecycle fixes — plus any future path that
slips past them — leaves residue:

  - kg_node_evidence rows whose node no longer exists,
  - kg_edge_evidence rows whose edge no longer exists,
  - ACTIVE kg_node_verdict rows naming a dead node,
  - Chroma edge-sentence vectors whose edge row is gone (the hourly
    embedding reconciler covers the NODE collections only).

Idempotent, cheap (set-based SQL + one chroma scan), safe to run any time.
First run clears the audited backlog (601 evidence orphans, 4,141 dead
verdicts); steady-state runs should find ~0 and anything persistent here
means a delete path regressed.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


def _table_exists(session, table_name: str) -> bool:
    return session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"),
        {"t": table_name},
    ).fetchone() is not None


def run(ctx=None) -> Dict[str, Any]:
    counts: Dict[str, Any] = {
        "node_evidence_orphans_deleted": 0,
        "edge_evidence_orphans_deleted": 0,
        "dead_verdicts_superseded": 0,
        "edge_vector_ghosts_removed": 0,
    }

    with get_db_manager().transaction(op="kg_maintenance.lifecycle_gc") as session:
        if _table_exists(session, "kg_node_evidence"):
            r = session.execute(text(
                "DELETE FROM kg_node_evidence WHERE NOT EXISTS "
                "(SELECT 1 FROM kg_node_metadata n WHERE n.id = kg_node_evidence.node_id)"
            ))
            counts["node_evidence_orphans_deleted"] = r.rowcount or 0

        if _table_exists(session, "kg_edge_evidence"):
            r = session.execute(text(
                "DELETE FROM kg_edge_evidence WHERE NOT EXISTS "
                "(SELECT 1 FROM kg_edge_metadata e WHERE e.id = kg_edge_evidence.edge_id)"
            ))
            counts["edge_evidence_orphans_deleted"] = r.rowcount or 0

        if _table_exists(session, "kg_node_verdict"):
            r = session.execute(
                text(
                    "UPDATE kg_node_verdict SET superseded_at = :now, "
                    "superseded_reason = 'lifecycle_gc: node deleted' "
                    "WHERE superseded_at IS NULL AND ("
                    "  NOT EXISTS (SELECT 1 FROM kg_node_metadata n WHERE n.id = kg_node_verdict.node_id_a)"
                    "  OR NOT EXISTS (SELECT 1 FROM kg_node_metadata n WHERE n.id = kg_node_verdict.node_id_b))"
                ),
                {"now": utc_now().isoformat()},
            )
            counts["dead_verdicts_superseded"] = r.rowcount or 0

    # Chroma edge-vector ghost sweep — outside the SQL transaction (chroma is
    # not transactional with SQLite; best-effort, the next run retries).
    try:
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        cm = get_chroma_manager()
        edge_collection = getattr(cm, "edge_collection", None)
        if edge_collection is not None:
            chroma_ids = [str(i) for i in (edge_collection.get().get("ids") or [])]
            if chroma_ids:
                with get_db_manager().read_session() as s:
                    live = {
                        str(r[0]) for r in s.execute(
                            text("SELECT id FROM kg_edge_metadata")
                        ).fetchall()
                    }
                ghosts = [cid for cid in chroma_ids if cid not in live]
                for cid in ghosts:
                    cm.delete_edge_embedding(cid)
                counts["edge_vector_ghosts_removed"] = len(ghosts)
    except Exception as exc:
        logger.error("[lifecycle_gc] chroma edge-ghost sweep failed (next run retries): %s", exc)

    logger.info("[lifecycle_gc] %s", counts)
    return counts
