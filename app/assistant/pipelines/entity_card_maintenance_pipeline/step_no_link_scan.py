"""
Step: no_link_scan

Finds active entity cards with no source_node_id at all — they have no KG
anchor.  Most were created manually or before provenance tracking was added.
Suggested action: review (do not auto-deactivate — many are still valid).
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

    session = get_session()
    try:
        rows = (
            session.query(EntityCard.id, EntityCard.entity_name, EntityCard.entity_type)
            .filter(
                EntityCard.is_active == True,
                EntityCard.source_node_id.is_(None),
            )
            .all()
        )
        cards = [(str(r.id), r.entity_name or "", r.entity_type or "") for r in rows]
    finally:
        session.close()

    new_findings = 0
    for card_id, name, etype in cards:
        _, created = upsert_finding(
            finding_type="no_kg_link",
            entity_card_id=card_id,
            suggested_action="review",
            reason="Card has no source_node_id — cannot be traced to a KG node.",
            confidence=0.9,
            priority="low",
            evidence={"entity_name": name, "entity_type": etype},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[no_link_scan] scanned=%d new_findings=%d",
        len(cards), new_findings,
    )
    return {"scanned": len(cards), "new_findings": new_findings}
