"""
Step: orphan_scan

Identifies nodes that have zero edges.  Each orphan gets a "delete" finding.
Self-manages a short-lived read session; findings are written via the store
(which also self-manages its session).
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

    # Read phase — short-lived session
    session = get_session()
    try:
        connected_ids = (
            session.query(Edge.source_id)
            .union(session.query(Edge.target_id))
            .subquery()
        )
        orphan_nodes = (
            session.query(Node.id, Node.label, Node.node_type)
            .filter(Node.id.notin_(connected_ids))
            .all()
        )
        # Copy to plain tuples so nothing leaks across session boundary
        orphans = [(str(r.id), r.label or "", r.node_type or "") for r in orphan_nodes]
    finally:
        session.close()

    # Write phase — store opens its own session per finding
    new_findings = 0
    for node_id, label, node_type in orphans:
        _, created = upsert_finding(
            finding_type="orphan_node",
            primary_node_id=node_id,
            suggested_action="delete",
            reason=f"Node '{label}' (type={node_type}) has no edges.",
            confidence=0.95,
            priority="low",
            agent_name="orphan_scan",
            evidence={"label": label, "node_type": node_type},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[orphan_scan] Scanned %d orphan nodes, %d new findings",
        len(orphans),
        new_findings,
    )
    return {"scanned": len(orphans), "new_findings": new_findings}
