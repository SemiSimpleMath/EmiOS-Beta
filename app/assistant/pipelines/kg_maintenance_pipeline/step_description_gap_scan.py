"""
Step: description_gap_scan

Finds nodes that have no description field.  Only flags nodes with at least
one edge (true orphans are handled separately by orphan_scan).
Self-manages a short-lived read session; findings are written via the store.
"""
from __future__ import annotations

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    session = get_session()
    try:
        # Only nodes that have at least one edge (orphans handled elsewhere)
        connected_ids = (
            session.query(Edge.source_id)
            .union(session.query(Edge.target_id))
            .subquery()
        )
        nodes = (
            session.query(Node.id, Node.label, Node.node_type)
            .filter(
                (Node.description.is_(None)) | (Node.description == ""),
                Node.id.in_(connected_ids),
            )
            .all()
        )
        candidates = [(str(r.id), r.label or "", r.node_type or "") for r in nodes]
    finally:
        session.close()

    new_findings = 0
    for node_id, label, node_type in candidates:
        _, created = upsert_finding(
            finding_type="missing_description",
            primary_node_id=node_id,
            suggested_action="add_description",
            reason=f"Node '{label}' (type={node_type}) has no description.",
            confidence=1.0,
            priority="low",
            agent_name="description_gap_scan",
            evidence={"label": label, "node_type": node_type},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[description_gap_scan] Scanned %d nodes with missing descriptions, %d new findings",
        len(candidates),
        new_findings,
    )
    return {"scanned": len(candidates), "new_findings": new_findings}
