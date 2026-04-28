"""
Step: low_confidence_scan

Finds active entity cards where the generator assigned a confidence < 0.5.
Low confidence suggests the LLM was uncertain about the entity — the card may
contain inaccurate facts.
Suggested action: review.
"""
from __future__ import annotations

from app.assistant.entity_card_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

CONFIDENCE_THRESHOLD = 0.5


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.entity_management.entity_cards import EntityCard

    session = get_session()
    try:
        rows = (
            session.query(EntityCard.id, EntityCard.entity_name, EntityCard.entity_type, EntityCard.confidence)
            .filter(
                EntityCard.is_active == True,
                EntityCard.confidence.isnot(None),
                EntityCard.confidence < CONFIDENCE_THRESHOLD,
            )
            .all()
        )
        cards = [
            (str(r.id), r.entity_name or "", r.entity_type or "", float(r.confidence))
            for r in rows
        ]
    finally:
        session.close()

    new_findings = 0
    for card_id, name, etype, conf in cards:
        _, created = upsert_finding(
            finding_type="low_confidence",
            entity_card_id=card_id,
            suggested_action="review",
            reason=f"Generator confidence {conf:.2f} is below threshold {CONFIDENCE_THRESHOLD}.",
            confidence=conf,
            priority="medium",
            evidence={"entity_name": name, "entity_type": etype, "confidence": conf},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[low_confidence_scan] scanned=%d new_findings=%d",
        len(cards), new_findings,
    )
    return {"scanned": len(cards), "new_findings": new_findings}
