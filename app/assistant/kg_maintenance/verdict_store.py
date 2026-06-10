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

from datetime import datetime, timedelta, timezone
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

# Nano-tier (triage) verdicts expire: a wrong nano "distinct" must not
# suppress a true duplicate forever (audit P1.4; 90d default per P4.3).
# Expiry happens in the LOADER — rows are never deleted, and verdicts from
# the investigator / cluster resolver / user stay permanent.
NANO_TRIAGE_DECIDED_BY = "kg_maintenance::duplicate_triage"
NANO_VERDICT_TTL_DAYS = 90


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

    node_ids: 1-2 ids. For pairwise verdicts (verdict_type='distinct'),
    pass exactly 2; the helper canonicalizes their order. For single-node
    verdicts, pass 1. More than 2 is rejected (the row holds at most a
    pair; the final_answer form enforces the same cap).

    verdict_type is a CLOSED vocabulary (VALID_VERDICT_TYPES) — unknown
    types are rejected with None, not accepted, because downstream
    filters match on these exact strings.

    Uniqueness (audit P1.4): at most one ACTIVE verdict per
    (node_id_a, node_id_b, verdict_type). An existing active row for the
    same key is superseded by the new write — newer investigation wins.

    Returns None if the input is malformed (no node_ids, blank memo,
    unknown type, >2 ids).
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
        # Active-uniqueness: supersede any live verdict for the same
        # (a, b, type) so exactly one active row answers each question.
        # Supersede (not skip) — the new verdict carries newer evidence.
        stale_q = (
            session.query(KGNodeVerdict)
            .filter(
                KGNodeVerdict.node_id_a == a,
                (KGNodeVerdict.node_id_b == b) if b is not None
                else KGNodeVerdict.node_id_b.is_(None),
                KGNodeVerdict.verdict_type == verdict_type,
                KGNodeVerdict.superseded_at.is_(None),
            )
        )
        for stale in stale_q.all():
            stale.superseded_at = utc_now()
            stale.superseded_reason = (
                f"superseded by newer {verdict_type} verdict from {decided_by}"
            )

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


def record_distinct_pairs_bulk(
    pairs: List[Tuple[str, str]] | List[Tuple[str, str, Optional[str]]],
    *,
    decided_by: str,
    memo: str,
    reasoning: Optional[str] = None,
) -> int:
    """Persist a batch of pairwise 'distinct' verdicts in a single transaction.

    Used by duplicate_scan's triage step so the ~95-97% of candidate pairs
    the nano LLM marks "distinct" don't have to be re-triaged on every
    maintenance run. Without this, the scan paid full LLM cost on the same
    ~24k pairs week after week.

    Each pair is (a, b) or (a, b, reasoning) — the optional third element
    persists the decider's per-pair reason into the reasoning column
    (audit P1.4: these are long-lived decisions and used to carry no audit
    trail). The shared ``reasoning`` kwarg applies to pairs without one.

    Skips pairs that already have a non-superseded 'distinct' verdict
    (no point bloating the verdict table with duplicate rows).

    Returns the count of newly-inserted verdicts.
    """
    canon: List[Tuple[str, str]] = []
    reason_by_pair: dict[Tuple[str, str], str] = {}
    for p in pairs:
        a, b = p[0], p[1]
        if not a or not b:
            continue
        ca, cb = canonical_pair(a, b)
        if cb is None:
            continue
        canon.append((ca, cb))
        per_pair = p[2] if len(p) > 2 else None
        if per_pair:
            reason_by_pair[(ca, cb)] = str(per_pair)
    if not canon:
        return 0

    with get_db_manager().transaction(op="verdict_store.record_distinct_bulk") as session:
        # One pass to learn which pairs already have an active distinct verdict.
        # We compare on (a, b) tuples; the canonical_pair call above ensures
        # a < b so the comparison is symmetric.
        pair_set = set(canon)
        existing = (
            session.query(KGNodeVerdict.node_id_a, KGNodeVerdict.node_id_b)
            .filter(
                KGNodeVerdict.verdict_type == "distinct",
                KGNodeVerdict.superseded_at.is_(None),
                KGNodeVerdict.node_id_a.in_({a for a, _ in canon}),
                KGNodeVerdict.node_id_b.in_({b for _, b in canon}),
            )
            .all()
        )
        existing_set = {(a, b) for a, b in existing}
        to_insert = [(a, b) for (a, b) in pair_set if (a, b) not in existing_set]

        n_new = 0
        for a, b in to_insert:
            session.add(
                KGNodeVerdict(
                    node_id_a=a,
                    node_id_b=b,
                    verdict_type="distinct",
                    memo=memo,
                    reasoning=reason_by_pair.get((a, b), reasoning),
                    decided_by=decided_by,
                )
            )
            n_new += 1
        session.flush()

    if n_new:
        logger.info(
            "[verdict_store] recorded %d distinct verdicts in bulk (decided_by=%s)",
            n_new, decided_by,
        )
    return n_new


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


def load_distinct_verdicts_among(node_ids: List[str]) -> List[Tuple[str, str, str, str]]:
    """Active 'distinct' verdicts whose BOTH endpoints are in ``node_ids``.

    Returns [(node_id_a, node_id_b, memo, decided_by), ...]. Used by the
    proposal_promoter ("Reader #3", audit P2.5): when two merge CANDIDATES
    were already ruled distinct from each other, the node_merger should
    know it before binding a new observation into either twin. The memos
    are injected into the candidate payload — never used to silently drop
    a candidate (deterministic proposes, LLM decides).
    """
    ids = [i for i in (node_ids or []) if i]
    if len(ids) < 2:
        return []
    with get_db_manager().read_session() as session:
        rows = (
            session.query(
                KGNodeVerdict.node_id_a, KGNodeVerdict.node_id_b,
                KGNodeVerdict.memo, KGNodeVerdict.decided_by,
            )
            .filter(KGNodeVerdict.verdict_type == "distinct")
            .filter(KGNodeVerdict.superseded_at.is_(None))
            .filter(KGNodeVerdict.node_id_a.in_(ids))
            .filter(KGNodeVerdict.node_id_b.in_(ids))
            .all()
        )
    return [(a, b, memo or "", decided_by or "") for a, b, memo, decided_by in rows]


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

    Nano-triage verdicts older than NANO_VERDICT_TTL_DAYS are treated as
    expired here (audit P1.4): a wrong nano "distinct" must not suppress a
    true duplicate forever. The rows stay in the table (the investigator's
    brief still shows them as history); only this scan filter ages them
    out. Investigator / cluster-resolver / user verdicts never expire.
    """
    nano_cutoff = utc_now() - timedelta(days=NANO_VERDICT_TTL_DAYS)
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGNodeVerdict.node_id_a, KGNodeVerdict.node_id_b)
            .filter(KGNodeVerdict.verdict_type == "distinct")
            .filter(KGNodeVerdict.superseded_at.is_(None))
            .filter(KGNodeVerdict.node_id_b.isnot(None))
            .filter(
                or_(
                    KGNodeVerdict.decided_by != NANO_TRIAGE_DECIDED_BY,
                    KGNodeVerdict.created_at >= nano_cutoff,
                )
            )
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
