"""
Step: disambiguation_scan

Disambiguation nodes are attachment points: mentions whose true referent
is unknown bind to them, and their edges accumulate until an
investigator re-points each edge to the right node (kg_repoint_edge).
This scan is the drain trigger — every Disambiguation node carrying at
least one edge gets a "disambiguation_backlog" finding.

Edge-less Disambiguation nodes are the resting state (waiting for
ambiguous mentions) and raise nothing.

Self-manages a short-lived read session; findings are written via the
store (which also self-manages its session).
"""
from __future__ import annotations

from sqlalchemy import or_

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
    from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE

    # Read phase — short-lived session
    session = get_session()
    try:
        dis_rows = (
            session.query(Node.id, Node.label)
            .filter(Node.node_type == DISAMBIGUATION_NODE_TYPE)
            .all()
        )
        backlog: list[tuple[str, str, int]] = []
        for r in dis_rows:
            node_id = str(r.id)
            edge_count = (
                session.query(Edge.id)
                .filter(or_(Edge.source_id == node_id, Edge.target_id == node_id))
                .count()
            )
            if edge_count > 0:
                backlog.append((node_id, r.label or "", edge_count))
    finally:
        session.close()

    # Write phase — store opens its own session per finding
    new_findings = 0
    for node_id, label, edge_count in backlog:
        _, created = upsert_finding(
            finding_type="disambiguation_backlog",
            primary_node_id=node_id,
            suggested_action="repoint_edges",
            reason=(
                f"Disambiguation node '{label}' has accumulated {edge_count} "
                f"edge(s) from mentions whose referent was unknown at write "
                f"time. Determine each edge's true referent and re-point it."
            ),
            confidence=1.0,
            priority="medium",
            agent_name="disambiguation_scan",
            evidence={"label": label, "edge_count": edge_count},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[disambiguation_scan] %d Disambiguation nodes scanned, %d with "
        "backlog, %d new findings",
        len(dis_rows), len(backlog), new_findings,
    )
    return {"scanned": len(dis_rows), "with_backlog": len(backlog), "new_findings": new_findings}
