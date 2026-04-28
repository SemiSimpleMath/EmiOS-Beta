"""
Step: broken_link_scan

Finds entity cards whose source_node_id is set but the referenced KG node
no longer exists (node was deleted from kg_node_metadata / Node table).
These cards are dangling — suggested action: deactivate.
"""
from __future__ import annotations

from app.assistant.entity_card_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.entity_management.entity_cards import EntityCard
    from app.assistant.kg.db.knowledge_graph_db import Node

    session = get_session()
    try:
        # Cards that have a source_node_id but no matching Node row.
        linked = (
            session.query(EntityCard.id, EntityCard.entity_name, EntityCard.entity_type, EntityCard.source_node_id)
            .filter(
                EntityCard.is_active == True,
                EntityCard.source_node_id.isnot(None),
            )
            .all()
        )
        linked_ids = [str(r.source_node_id) for r in linked]
        if not linked_ids:
            return {"scanned": 0, "new_findings": 0}

        existing_node_ids = {
            str(row[0])
            for row in session.query(Node.id).filter(Node.id.in_(linked_ids)).all()
        }
        broken = [
            (str(r.id), r.entity_name or "", r.entity_type or "", str(r.source_node_id))
            for r in linked
            if str(r.source_node_id) not in existing_node_ids
        ]
    finally:
        session.close()

    new_findings = 0
    for card_id, name, etype, node_id in broken:
        _, created = upsert_finding(
            finding_type="broken_kg_link",
            entity_card_id=card_id,
            suggested_action="deactivate",
            reason=f"source_node_id {node_id!r} not found in KG node table.",
            confidence=0.99,
            priority="high",
            evidence={"entity_name": name, "entity_type": etype, "source_node_id": node_id},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[broken_link_scan] scanned=%d broken=%d new_findings=%d",
        len(linked), len(broken), new_findings,
    )
    return {"scanned": len(linked), "new_findings": new_findings}
