"""Read/write helpers for the kg_node_verdict table.

A verdict is a durable record of "we looked at this and concluded no
KG mutation is needed." Three readers consult this table:

1. duplicate_scan — drops candidate pairs with a prior 'distinct' verdict
   before they reach the LLM duplicate detector.
2. finding_brief.build_finding_brief — surfaces prior verdicts on the
   subject node ids so the investigator can see what was decided before.
3. proposal_promoter (planned) — drops merge candidates with a 'distinct'
   verdict before they reach the merge LLM.

Pairwise verdicts are stored with canonical lexicographic ordering
(node_id_a < node_id_b) so a single index lookup suffices.

Single-node verdicts ('verified', 'false_positive', 'irrelevant',
'obsolete') store the subject in node_id_a with node_id_b NULL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_

from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


VALID_VERDICT_TYPES = {
    "distinct",          # pairwise: two nodes are NOT the same thing
    "verified",          # single-node: data is correct as-is
    "false_positive",    # single-node: flagged issue isn't real
    "obsolete",          # single-node: finding refers to since-superseded data
    "irrelevant",        # single-node: finding type doesn't apply
}


def canonical_pair(node_id_a: str, node_id_b: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return (a, b) in canonical order: a < b lexicographically.

    - Single-node verdicts (b is None or empty) pass through with b=None.
    - Self-pair (a == b) collapses to single-node form (a, None) so
      callers can't write a (X, X) pair-row that no lookup will hit.
    """
    if not node_id_b or node_id_a == node_id_b:
        return node_id_a, None
    if node_id_a <= node_id_b:
        return node_id_a, node_id_b
    return node_id_b, node_id_a


def record_verdict(
    *,
    verdict_type: str,
    memo: str,
    node_ids: List[str],
    decided_by: str,
    reasoning: Optional[str] = None,
    source_finding_id: Optional[str] = None,
    confidence: Optional[float] = None,
) -> Optional[str]:
    """Insert a new verdict row. Returns the new verdict's id.

    node_ids: 1-3 ids. For pairwise verdicts (verdict_type='distinct'),
    pass exactly 2; the helper canonicalizes their order. For single-node
    verdicts, pass 1 (or more — only the first is used as node_id_a; the
    rest are ignored since the schema is single+pair).

    Caller responsibilities:
    - Validate verdict_type in VALID_VERDICT_TYPES (we log a warning
      but accept unknown types so the LLM can extend the vocabulary
      without blocking the write).
    - Pass a non-empty memo and at least one node_id.

    Returns None if the input is malformed (no node_ids, blank memo).
    """
    if not node_ids:
        logger.warning("[verdict_store] record_verdict skipped — no node_ids")
        return None
    memo = (memo or "").strip()
    if not memo:
        logger.warning("[verdict_store] record_verdict skipped — empty memo")
        return None

    # Closed vocabulary — any unknown type is a contract violation, not
    # an extension point. The downstream filters (duplicate_scan,
    # proposal_promoter) match on these strings; silently widening the
    # set corrupts those filters.
    if verdict_type not in VALID_VERDICT_TYPES:
        logger.error(
            "[verdict_store] unknown verdict_type %r — rejecting write. "
            "Allowed: %s",
            verdict_type, sorted(VALID_VERDICT_TYPES),
        )
        return None

    # The schema permits 1-3 ids. The store row holds at most a pair, so
    # 3+ ids is a contract violation we should flag rather than silently
    # truncate. Caller (finding_processor) is expected to canonicalize
    # to (a) one single-node verdict, (b) one pair, or (c) split into
    # multiple verdicts before calling.
    if len(node_ids) > 2:
        logger.error(
            "[verdict_store] record_verdict refusing %d-id verdict; "
            "schema row holds at most a pair. ids=%s",
            len(node_ids), node_ids,
        )
        return None

    a = node_ids[0]
    b = node_ids[1] if len(node_ids) >= 2 else None
    a, b = canonical_pair(a, b)

    with get_db_manager().transaction(op="verdict_store.record") as session:
        v = KGNodeVerdict(
            node_id_a=a,
            node_id_b=b,
            verdict_type=verdict_type,
            memo=memo,
            reasoning=reasoning,
            source_finding_id=source_finding_id,
            decided_by=decided_by,
            confidence=confidence,
        )
        session.add(v)
        session.flush()
        vid = v.id

    logger.info(
        "[verdict_store] recorded %s verdict %s for (%s, %s)",
        verdict_type, vid, a, b,
    )
    return vid


def get_verdicts_for_pair(
    node_id_a: str,
    node_id_b: str,
    *,
    verdict_type: Optional[str] = None,
    include_superseded: bool = False,
) -> List[KGNodeVerdict]:
    """Return verdicts that name BOTH of the given node ids as a pair.

    Order-insensitive: caller can pass (X, Y) or (Y, X) — we canonicalize.
    """
    a, b = canonical_pair(node_id_a, node_id_b)
    if not b:
        return []

    with get_db_manager().read_session() as session:
        q = session.query(KGNodeVerdict).filter(
            and_(
                KGNodeVerdict.node_id_a == a,
                KGNodeVerdict.node_id_b == b,
            )
        )
        if verdict_type is not None:
            q = q.filter(KGNodeVerdict.verdict_type == verdict_type)
        if not include_superseded:
            q = q.filter(KGNodeVerdict.superseded_at.is_(None))
        rows = q.order_by(KGNodeVerdict.created_at.desc()).all()
        # Detach so callers can use after the session closes.
        for r in rows:
            session.expunge(r)
        return rows


def get_verdicts_for_node(
    node_id: str,
    *,
    verdict_type: Optional[str] = None,
    include_superseded: bool = False,
    limit: int = 20,
) -> List[KGNodeVerdict]:
    """Return any verdict touching the given node id (single OR pairwise).

    Useful for the investigator's brief: "what was previously decided
    about this node?"
    """
    if not node_id:
        return []
    with get_db_manager().read_session() as session:
        q = session.query(KGNodeVerdict).filter(
            or_(
                KGNodeVerdict.node_id_a == node_id,
                KGNodeVerdict.node_id_b == node_id,
            )
        )
        if verdict_type is not None:
            q = q.filter(KGNodeVerdict.verdict_type == verdict_type)
        if not include_superseded:
            q = q.filter(KGNodeVerdict.superseded_at.is_(None))
        rows = q.order_by(KGNodeVerdict.created_at.desc()).limit(limit).all()
        for r in rows:
            session.expunge(r)
        return rows


def is_pair_marked_distinct(node_id_a: str, node_id_b: str) -> bool:
    """Convenience predicate for the duplicate-scan filter and
    proposal_promoter merge filter.

    For batch use, prefer ``load_distinct_pairs()`` + local set membership
    — calling this in a hot loop opens one read session per pair.
    """
    return bool(
        get_verdicts_for_pair(node_id_a, node_id_b, verdict_type="distinct")
    )


def load_distinct_pairs() -> set[Tuple[str, str]]:
    """One-shot: fetch every active 'distinct' verdict's canonical pair.

    Returns a set of ``(node_id_a, node_id_b)`` tuples in canonical order
    so callers can do `(a, b) in distinct_pairs` after canonicalizing
    locally. Used by duplicate_scan to filter hundreds of candidate
    pairs against the verdict table without N round-trips.
    """
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGNodeVerdict.node_id_a, KGNodeVerdict.node_id_b)
            .filter(KGNodeVerdict.verdict_type == "distinct")
            .filter(KGNodeVerdict.superseded_at.is_(None))
            .filter(KGNodeVerdict.node_id_b.isnot(None))
            .all()
        )
    # Stored canonically (a < b), so no re-sort needed.
    return {(a, b) for a, b in rows if a and b}


def supersede_verdict(verdict_id: str, *, reason: str) -> bool:
    """Mark a verdict as superseded (soft-delete). Returns True if a row
    was updated."""
    with get_db_manager().transaction(op="verdict_store.supersede") as session:
        v = session.query(KGNodeVerdict).filter(KGNodeVerdict.id == verdict_id).first()
        if v is None:
            return False
        v.superseded_at = utc_now()
        v.superseded_reason = reason
    return True
