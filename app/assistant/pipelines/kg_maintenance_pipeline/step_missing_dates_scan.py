"""
Step: missing_dates_scan

Surface State and Event nodes whose category implies a bounded period but
which have no start_date set, so the user can be asked to fill them in
gradually (1-3 per day via the daily routine, see
routine_functions.kg_date_gap_drain).

Distinct from the broader "this State has no dates" lint that the wiki
renderer already emits as KG GAP comments — that fires for every
unboundend State too. This scanner restricts to categories where missing
dates is actionable (parenthood, ownership, residence, employment, ...).

Each candidate gets a score = sum of the top-2 pageranks of connected
Entity nodes. States directly connected to the primary user are
auto-promoted to ``priority='high'``. The score bins:
  - top-2 sum >= median(top-2 sums)   → high
  - >= 25th percentile                  → medium
  - else                                → low

Idempotent via ``upsert_finding``: nodes that already have a pending
state_missing_dates finding are skipped; rejected/executed don't block,
so users can dismiss a question and the scanner won't immediately re-raise
it (rejected rows survive a future scan only if the user explicitly
re-opens the queue).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.identity_names import get_required_primary_user_name
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


# Categories where a State or Event genuinely has a beginning the user can
# answer to. Excludes recurring/instantaneous (eating, walking, conversation),
# open-ended preference/belief categories (preference, belief, opinion), and
# computed/auto categories (age, language proficiency).
#
# Match is case-insensitive and matches by exact category string.
BOUNDED_CATEGORIES: frozenset[str] = frozenset({
    # State categories
    "ownership",
    "residence",
    "parenthood",
    "new_parenthood",
    "marriage",
    "employment",
    "education",
    "friendship",
    "sibling relationship",
    "membership",
    "partnership",
    "career",
    "school enrollment",
    "home country",
    # Event categories that almost always have a known date
    "relocation",
    "wedding",
    "birth",
    "graduation",
    "adoption",
    "purchase",
    "death",
})


def _normalize_category(c: Optional[str]) -> str:
    return (c or "").strip().lower()


def _bin_priority(score: float, distribution: List[float]) -> str:
    """Map a score to high/medium/low using the distribution's percentiles.

    distribution is the sorted list of all candidate scores. We use the
    *median* as the high cutoff and the *25th percentile* as the medium
    cutoff. Scores at-or-above median are high, between p25 and median are
    medium, below p25 are low. Edge cases: empty / 1-element distributions
    fall back to "medium".
    """
    n = len(distribution)
    if n == 0 or score is None:
        return "medium"
    if n < 4:
        # Too few candidates for percentile binning to be meaningful — give
        # everyone medium so the daily routine sees them in created_at order.
        return "medium"
    sorted_scores = sorted(distribution)
    p50 = sorted_scores[n // 2]
    p25 = sorted_scores[n // 4]
    if score >= p50:
        return "high"
    if score >= p25:
        return "medium"
    return "low"


def _top2_sum(pageranks: List[float]) -> float:
    """Sum of the two highest pageranks in the list. Captures 'two important
    people both involved' without rewarding hubs of mediocrities."""
    if not pageranks:
        return 0.0
    cleaned = sorted((p for p in pageranks if p is not None), reverse=True)
    if not cleaned:
        return 0.0
    return float(cleaned[0]) + (float(cleaned[1]) if len(cleaned) > 1 else 0.0)


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "candidates": int, "new_findings": int}."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    primary_user_name = ""
    primary_user_id: Optional[str] = None
    try:
        primary_user_name = get_required_primary_user_name()
    except Exception as exc:
        logger.warning("[missing_dates_scan] primary user name unresolved: %s", exc)

    # Read phase — collect candidate state/event nodes + their connected
    # entity pageranks in one short-lived session. Closing before the write
    # phase keeps the per-finding session opens cheap.
    session = get_session()
    try:
        if primary_user_name:
            row = (
                session.query(Node.id)
                .filter(Node.label == primary_user_name, Node.node_type == "Entity")
                .first()
            )
            if row:
                primary_user_id = str(row.id)

        candidates_q = (
            session.query(
                Node.id, Node.label, Node.node_type, Node.category, Node.description
            )
            .filter(
                Node.node_type.in_(["State", "Event"]),
                Node.start_date.is_(None),
            )
        )
        all_candidates = candidates_q.all()
        scanned = len(all_candidates)

        # Filter to bounded categories only — saves a lot of pagerank lookups.
        bounded = [
            (str(r.id), r.label or "", r.node_type or "", r.category or "", r.description or "")
            for r in all_candidates
            if _normalize_category(r.category) in BOUNDED_CATEGORIES
        ]

        # For each bounded candidate, collect the pagerank of every
        # connected Entity node (in/out edges, deduped) and remember whether
        # the primary user is among them.
        scored: List[Tuple[str, str, str, str, str, float, bool, List[str]]] = []
        for node_id, label, node_type, category, description in bounded:
            entity_rows = (
                session.query(Node.id, Node.label, Node.pagerank_score)
                .join(
                    Edge,
                    or_(
                        (Edge.source_id == node_id) & (Edge.target_id == Node.id),
                        (Edge.target_id == node_id) & (Edge.source_id == Node.id),
                    ),
                )
                .filter(Node.node_type == "Entity")
                .distinct()
                .all()
            )
            pageranks = [float(r.pagerank_score or 0.0) for r in entity_rows]
            score = _top2_sum(pageranks)
            connects_primary = primary_user_id is not None and any(
                str(r.id) == primary_user_id for r in entity_rows
            )
            entity_labels = [r.label or "" for r in entity_rows]
            scored.append((
                node_id, label, node_type, category, description,
                score, connects_primary, entity_labels,
            ))
    finally:
        session.close()

    if not scored:
        logger.info("[missing_dates_scan] no bounded-category candidates with NULL start_date")
        return {"scanned": scanned, "candidates": 0, "new_findings": 0}

    # Distribution for percentile binning. Auto-promoted (primary-user-connected)
    # rows still go through binning for tiebreaking among themselves.
    score_distribution = [s[5] for s in scored]

    new_findings = 0
    for (node_id, label, node_type, category, description,
         score, connects_primary, entity_labels) in scored:
        priority = "high" if connects_primary else _bin_priority(score, score_distribution)

        evidence: Dict[str, object] = {
            "label": label,
            "node_type": node_type,
            "category": category,
            "description": (description or "")[:300],
            "connected_entity_labels": entity_labels[:10],
            "top_2_pagerank_sum": round(score, 4),
            "connects_primary_user": connects_primary,
        }
        reason = (
            f"{node_type} '{label}' (category={category!r}) has no start_date — "
            f"connects {len(entity_labels)} entit{'y' if len(entity_labels) == 1 else 'ies'} "
            f"(top-2 PR sum {score:.3f}{', includes primary user' if connects_primary else ''})."
        )

        _, created = upsert_finding(
            finding_type="state_missing_dates",
            primary_node_id=node_id,
            suggested_action="ask_user",
            reason=reason,
            confidence=0.85,
            priority=priority,
            agent_name="missing_dates_scan",
            evidence=evidence,
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.info(
        "[missing_dates_scan] scanned=%d bounded_candidates=%d new_findings=%d",
        scanned, len(scored), new_findings,
    )
    return {"scanned": scanned, "candidates": len(scored), "new_findings": new_findings}
