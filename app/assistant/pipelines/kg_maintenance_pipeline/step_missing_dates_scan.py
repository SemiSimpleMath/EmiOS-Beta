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


# ── Curation (2026-06-12) ────────────────────────────────────────────────
# The original scanner auto-promoted anything touching the primary user to
# 'high' (nearly everything does — he OWNS the swivel chair) and binned the
# rest by percentile, so in a pool of trivia the least-trivial trivia still
# ranked high. Worth is now ABSOLUTE and based on the one signal that
# separates a swivel chair from a Berkeley degree: the semantic importance
# of what the state connects to, read through the importance module
# (effective_importance — never raw; thresholds live in
# importance.consumers like every other gate).
from app.assistant.importance import effective_importance
from app.assistant.importance.consumers import (
    DATE_GAP_WORTH_FLOOR as WORTH_FLOOR,
    date_gap_priority,
)


def _worth(own_effective: float, non_primary_entity_importances: List[float]) -> float:
    """worth(state) = max effective importance among connected NON-primary
    entities; a state whose only entity is the primary user falls back to
    its own effective importance."""
    cleaned = [float(v) for v in non_primary_entity_importances if v is not None]
    if cleaned:
        return max(cleaned)
    return float(own_effective or 0.0)


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
            session.query(Node)
            .filter(
                Node.node_type.in_(["State", "Event"]),
                Node.start_date.is_(None),
            )
        )
        all_candidates = candidates_q.all()
        scanned = len(all_candidates)

        # Filter to bounded categories only — saves a lot of entity lookups.
        bounded = [
            (str(n.id), n.label or "", n.node_type or "", n.category or "",
             n.description or "", effective_importance(n))
            for n in all_candidates
            if _normalize_category(n.category) in BOUNDED_CATEGORIES
        ]

        def _entity_context(node_id: str):
            entity_nodes = (
                session.query(Node)
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
            non_primary = [
                effective_importance(n) for n in entity_nodes
                if n.importance is not None and str(n.id) != primary_user_id
            ]
            connects_primary = primary_user_id is not None and any(
                str(n.id) == primary_user_id for n in entity_nodes
            )
            labels = [n.label or "" for n in entity_nodes]
            return non_primary, connects_primary, labels

        scored: List[Tuple[str, str, str, str, str, float, bool, List[str]]] = []
        below_floor_ids: List[str] = []
        for node_id, label, node_type, category, description, own_effective in bounded:
            non_primary, connects_primary, entity_labels = _entity_context(node_id)
            worth = _worth(own_effective, non_primary)
            if worth < WORTH_FLOOR:
                below_floor_ids.append(node_id)
                continue
            scored.append((
                node_id, label, node_type, category, description,
                worth, connects_primary, entity_labels,
            ))

        # Sweep: pending findings whose node now computes below the floor
        # (including pre-curation backlog) move to 'rejected' — the
        # designed don't-re-raise state.
        from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
        swept = 0
        pending_rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "state_missing_dates")
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        below_floor_set = set(below_floor_ids)
        scored_ids = {s[0] for s in scored}
        for f in pending_rows:
            pid = str(f.primary_node_id)
            if pid in scored_ids:
                continue
            if pid in below_floor_set:
                in_floor = True
            else:
                # Node not in this scan's candidate set (dated meanwhile,
                # category drift, ...) — leave those alone; only sweep
                # confirmed below-floor nodes.
                in_floor = False
            if in_floor:
                f.status = "rejected"
                f.execution_notes = (
                    f"curation floor: worth < {WORTH_FLOOR} — nobody wants to "
                    f"date-stamp low-importance objects (2026-06-12 curation)"
                )
                swept += 1
        if swept:
            session.commit()
    finally:
        session.close()

    if not scored:
        logger.info(
            "[missing_dates_scan] no candidates above the worth floor "
            "(below_floor=%d swept=%d)", len(below_floor_ids), swept,
        )
        return {"scanned": scanned, "candidates": 0, "new_findings": 0,
                "below_floor": len(below_floor_ids), "swept": swept}

    new_findings = 0
    for (node_id, label, node_type, category, description,
         worth, connects_primary, entity_labels) in scored:
        priority = date_gap_priority(worth)

        evidence: Dict[str, object] = {
            "label": label,
            "node_type": node_type,
            "category": category,
            "description": (description or "")[:300],
            "connected_entity_labels": entity_labels[:10],
            "worth": round(worth, 2),
            "connects_primary_user": connects_primary,
        }
        reason = (
            f"{node_type} '{label}' (category={category!r}) has no start_date — "
            f"connects {len(entity_labels)} entit{'y' if len(entity_labels) == 1 else 'ies'} "
            f"(worth {worth:.1f}{', includes primary user' if connects_primary else ''})."
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
        "[missing_dates_scan] scanned=%d above_floor=%d below_floor=%d swept=%d new_findings=%d",
        scanned, len(scored), len(below_floor_ids), swept, new_findings,
    )
    return {"scanned": scanned, "candidates": len(scored), "new_findings": new_findings,
            "below_floor": len(below_floor_ids), "swept": swept}
