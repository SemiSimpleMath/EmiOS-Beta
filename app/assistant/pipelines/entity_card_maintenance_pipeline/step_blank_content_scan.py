"""
Step: blank_content_scan

Finds active entity cards with empty or trivially short summaries, or with
an empty key_facts list.  These cards add no signal when injected into prompts.
Suggested action: review.
"""
from __future__ import annotations

from app.assistant.entity_card_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

_MIN_SUMMARY_LEN = 20


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.entity_management.entity_cards import EntityCard

    session = get_session()
    try:
        rows = (
            session.query(
                EntityCard.id,
                EntityCard.entity_name,
                EntityCard.entity_type,
                EntityCard.summary,
                EntityCard.key_facts,
            )
            .filter(EntityCard.is_active == True)
            .all()
        )
        cards = [
            (str(r.id), r.entity_name or "", r.entity_type or "", r.summary or "", r.key_facts)
            for r in rows
        ]
    finally:
        session.close()

    new_findings = 0
    scanned = 0
    for card_id, name, etype, summary, key_facts in cards:
        issues = []
        if len(summary.strip()) < _MIN_SUMMARY_LEN:
            issues.append(f"summary too short ({len(summary.strip())} chars)")
        if not key_facts:
            issues.append("key_facts is empty")

        if not issues:
            continue

        scanned += 1
        _, created = upsert_finding(
            finding_type="blank_content",
            entity_card_id=card_id,
            suggested_action="review",
            reason=f"Card content is missing or inadequate: {'; '.join(issues)}.",
            confidence=0.95,
            priority="medium",
            evidence={
                "entity_name": name,
                "entity_type": etype,
                "summary_length": len(summary.strip()),
                "has_key_facts": bool(key_facts),
                "issues": issues,
            },
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[blank_content_scan] checked=%d blank=%d new_findings=%d",
        len(cards), scanned, new_findings,
    )
    return {"scanned": scanned, "new_findings": new_findings}
