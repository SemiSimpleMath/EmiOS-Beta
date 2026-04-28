"""
Step: stale_content_scan

Finds active entity cards whose linked KG node was updated more than
STALE_DAYS after the card was last generated — meaning the card content may
be out of date relative to what the KG now knows about the entity.
Suggested action: review.
"""
from __future__ import annotations

from app.assistant.entity_card_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

STALE_DAYS = 30


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.entity_management.entity_cards import EntityCard
    from app.assistant.kg.db.knowledge_graph_db import Node

    session = get_session()
    try:
        linked = (
            session.query(
                EntityCard.id,
                EntityCard.entity_name,
                EntityCard.entity_type,
                EntityCard.source_node_id,
                EntityCard.updated_at,
            )
            .filter(
                EntityCard.is_active == True,
                EntityCard.source_node_id.isnot(None),
            )
            .all()
        )

        node_ids = [str(r.source_node_id) for r in linked]
        node_updated = {
            str(row.id): row.updated_at
            for row in session.query(Node.id, Node.updated_at)
            .filter(Node.id.in_(node_ids))
            .all()
        }

        stale = []
        for r in linked:
            node_up = node_updated.get(str(r.source_node_id))
            if node_up is None or r.updated_at is None:
                continue
            # Make both tz-naive for comparison if needed
            card_up = r.updated_at.replace(tzinfo=None) if r.updated_at.tzinfo else r.updated_at
            n_up = node_up.replace(tzinfo=None) if node_up.tzinfo else node_up
            delta_days = (n_up - card_up).days
            if delta_days >= STALE_DAYS:
                stale.append((
                    str(r.id),
                    r.entity_name or "",
                    r.entity_type or "",
                    str(r.source_node_id),
                    delta_days,
                ))
    finally:
        session.close()

    new_findings = 0
    for card_id, name, etype, node_id, delta in stale:
        _, created = upsert_finding(
            finding_type="stale_content",
            entity_card_id=card_id,
            suggested_action="review",
            reason=f"KG node updated {delta} days after card was last generated.",
            confidence=0.8,
            priority="low",
            evidence={
                "entity_name": name,
                "entity_type": etype,
                "source_node_id": node_id,
                "node_ahead_by_days": delta,
                "stale_threshold_days": STALE_DAYS,
            },
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[stale_content_scan] linked=%d stale=%d new_findings=%d",
        len(linked), len(stale), new_findings,
    )
    return {"scanned": len(stale), "new_findings": new_findings}
