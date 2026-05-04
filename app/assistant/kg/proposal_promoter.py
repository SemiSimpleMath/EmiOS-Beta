"""
Promoter for the redesigned claim_proposals (v2, group-based).

Walks pending proposals one at a time, evaluates each, and — when
``commit=True`` — applies the group to the KG atomically:

    For each proposal (in its own transaction):
        1. Resolve/create each node:
             - Entities / Concepts / Goals / Properties:
                 label + alias match. No match → auto-create.
             - States / Events:
                 participant-subset match on their outgoing-from-entity
                 edges (resolve those participants first, then match on
                 (participants_subset, valid_from, optionally label)).
                 No match → auto-create.
        2. For each edge:
             - Endpoints resolved via ClaimProposalNode.resolved_node_id.
             - Duplicate check on (source, predicate, target) in kg_edge_metadata.
             - Lock check: if target/source is locked AND the edge would
               topologically conflict (same-predicate different-target
               for a unique relation), hold the whole proposal.
             - Otherwise: create edge with created_from_proposal_id.

Dry-run by default — prints outcomes without writing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalNode, ClaimProposalEdge,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# Node types that match by label+aliases (entity-like identity).
ENTITY_LIKE_TYPES = {"Entity", "Concept", "Goal", "Property"}

# Node types that match by participants+valid_from (relationship-like).
RELATIONSHIP_LIKE_TYPES = {"State", "Event"}


# Placeholder label patterns — the extractor sometimes fabricates "Unknown
# X" / "Unspecified Y" nodes for unresolved references. These must never
# reach production KG. Defensive check duplicated on the writer side.
import re as _re
_PLACEHOLDER_LABEL_PATTERNS = [
    _re.compile(r"^unknown\b", _re.IGNORECASE),
    _re.compile(r"^unspecified\b", _re.IGNORECASE),
    _re.compile(r"^unnamed\b", _re.IGNORECASE),
    _re.compile(r"^unidentified\b", _re.IGNORECASE),
    _re.compile(r"\(unknown\)", _re.IGNORECASE),
]


def _is_placeholder_label(label: str) -> bool:
    s = (label or "").strip()
    if not s:
        return True
    return any(pat.search(s) for pat in _PLACEHOLDER_LABEL_PATTERNS)


# ---------------------------------------------------------------------------
# Node resolution
# ---------------------------------------------------------------------------

def _resolve_entity_like(session, label: str) -> Optional[Node]:
    """Match an Entity/Concept/Goal node by canonical label or alias.

    Prefers locked + higher pagerank when multiple match.

    NOT for Property — Property nodes are subject-scoped (each entity gets
    its own "Date of Birth" / "Phone Number" / etc.) and must use
    `_resolve_property` instead. Label-only match would otherwise glob
    every "Date of Birth" mention into a single shared node, attaching
    unrelated people as participants.
    """
    if not label:
        return None
    label_lower = label.lower().strip()
    if not label_lower:
        return None

    hit = (
        session.query(Node)
        .filter(func.lower(Node.label) == label_lower)
        .filter(Node.node_type != "Property")
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )
    if hit:
        return hit

    like_pat = f'%"{label_lower}"%'
    return (
        session.query(Node)
        .filter(Node.node_type != "Property")
        .filter(func.lower(func.coalesce(func.cast(Node.aliases, type_=__import__("sqlalchemy").String), "")).like(like_pat))
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )


def _resolve_property(
    session, label: str, subject_kg_ids: Set[str],
) -> Optional[Node]:
    """Match a Property node by label AND shared subject.

    Properties are subject-scoped: "Date of Birth" for Jouko is a
    different node from "Date of Birth" for Jaime. The resolver must
    match BOTH the label AND at least one subject (any entity connected
    to the candidate Property via any edge) before merging.

    Without this, generic property labels (Date of Birth, Place of
    Birth, Phone Number, Email Address, Age, etc.) become global magnet
    nodes that all entities collide on.

    Falls back to None if no subject_kg_ids — without a subject we can't
    safely scope the match, so we let the caller create a new node.
    """
    if not label or not subject_kg_ids:
        return None
    label_lower = label.lower().strip()
    if not label_lower:
        return None
    subject_list = list(subject_kg_ids)

    # Candidate Property nodes by label.
    cands = (
        session.query(Node)
        .filter(func.lower(Node.label) == label_lower)
        .filter(Node.node_type == "Property")
        .all()
    )
    if not cands:
        return None
    cand_ids = [c.id for c in cands]

    # Find which of those candidates share an edge with any subject.
    matching_cand_ids: Set[str] = set()
    rows = (
        session.query(Edge.source_id, Edge.target_id)
        .filter(or_(
            Edge.source_id.in_(cand_ids), Edge.target_id.in_(cand_ids),
        ))
        .filter(or_(
            Edge.source_id.in_(subject_list), Edge.target_id.in_(subject_list),
        ))
        .all()
    )
    for src, tgt in rows:
        if src in cand_ids and tgt in subject_list:
            matching_cand_ids.add(src)
        elif tgt in cand_ids and src in subject_list:
            matching_cand_ids.add(tgt)
    if not matching_cand_ids:
        return None

    # Pick highest-priority match (locked + pagerank).
    return (
        session.query(Node)
        .filter(Node.id.in_(matching_cand_ids))
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )


_EVENT_DATE_TOLERANCE_DAYS = 7  # events more than this many days apart → different instances

# Minimum Jaccard score (intersection / union of participant sets) for a
# State/Event/Goal candidate to even reach the LLM merger. Below this, the
# candidate's only overlap with the new proposal is via a small handful of
# hub-y participants ("Jukka's children", "Jukka") and is not meaningful
# evidence of being the same instance — every kid-related event would
# otherwise look related to every other kid-related event.
#
# 0.5 chosen so that majority of either participant set must overlap; this
# is what would have caught the 2026-05-03 Performance over-merge
# (jaccard 1/6 ≈ 0.17). Tunable.
_MIN_JACCARD_FOR_LLM_CONSIDERATION = 0.5


def _score_candidates_by_participant_overlap(
    candidates_with_participants: list,
    new_participant_ids: set,
) -> list:
    """Jaccard-score State/Event candidates by participant overlap with a
    new proposal's participants. Drops candidates with zero overlap.

    Args:
        candidates_with_participants: list of (cand_node, set_of_participant_ids).
            cand_node is anything with an `id` attribute (a Node instance in
            production; a stub in tests).
        new_participant_ids: set of KG node ids that the new proposal
            connects to as participants.

    Returns:
        list of {"node", "overlap", "jaccard"} dicts, sorted desc by
        (jaccard, overlap).
    """
    scored: list = []
    for cand, cand_parts in candidates_with_participants:
        if not cand_parts:
            continue
        inter = cand_parts & new_participant_ids
        if not inter:
            continue
        union = cand_parts | new_participant_ids
        scored.append({
            "node": cand,
            "overlap": len(inter),
            "jaccard": len(inter) / max(1, len(union)),
            # The actual intersection set — used by the hub-weighted
            # overlap filter to weight by participant degree.
            "intersection": set(inter),
        })
    scored.sort(key=lambda s: (s["jaccard"], s["overlap"]), reverse=True)
    return scored


_DEFAULT_MIN_WEIGHTED_OVERLAP_SCORE = 0.1


def _filter_candidates_by_weighted_overlap(
    scored: list,
    entity_degrees: dict,
    min_weighted_score: float = _DEFAULT_MIN_WEIGHTED_OVERLAP_SCORE,
) -> list:
    """Decide-not-a-match for State/Event/Goal candidates whose participant
    overlap is composed only of high-degree hub entities.

    Why: a sharing of "Jukka's children" (degree ~hundreds) carries near-
    zero evidence of being the same instance — every kid-related event
    involves them. Sharing a low-degree specific entity ("Drowsy
    Chaperone", degree ~3) is strong evidence. Inverse-degree weighting
    captures this: weight(entity) = 1.0 / max(1, degree(entity)).

    The new proposal's participant fingerprint is implied by the scored
    candidates (they were scored against it). Each candidate's
    ``"node"._participant_intersection`` field, populated by
    ``_score_candidates_by_participant_overlap``, names the overlapping
    entity ids; we sum their weights.

    Args:
        scored: candidate list as returned by
            ``_score_candidates_by_participant_overlap``. Each item may
            carry an ``"intersection"`` key (a set of node_ids of shared
            participants) — populated by the scorer when this filter is
            in the chain. If absent, the filter is a no-op for that
            candidate (kept).
        entity_degrees: dict mapping participant node_id → edge count.
            Higher = more hub-y. Computed in the read phase of
            ``_prepare_proposal_plan``.
        min_weighted_score: minimum sum of inverse-degrees across the
            shared participant set. Defaults to
            ``_DEFAULT_MIN_WEIGHTED_OVERLAP_SCORE``.

    Returns:
        Filtered scored list, same shape.
    """
    kept: list = []
    for s in scored:
        intersection = s.get("intersection")
        if not intersection:
            # No intersection info recorded — be lenient (keep). Caller
            # is responsible for populating "intersection" when this
            # filter is in the chain.
            kept.append(s)
            continue
        weighted = 0.0
        for entity_id in intersection:
            degree = entity_degrees.get(entity_id, 1)
            weighted += 1.0 / max(1, int(degree or 1))
        if weighted >= min_weighted_score:
            kept.append(s)
    return kept


def _filter_candidates_by_label_equality(
    scored: list,
    new_label: str,
) -> list:
    """Decide-not-a-match for candidates whose label differs from the new
    proposal's label (case-insensitive, trimmed equality).

    Why: labels are intentionally drawn from a narrow standardized set,
    so SAME label is weak evidence (the narrow set conflates many distinct
    things — still need LLM). But DIFFERENT labels are strong evidence:
    the standardization wouldn't put truly-related instances in different
    buckets. So label-mismatch is a hard reject without LLM.

    Args:
        scored: candidate list as returned by
            ``_score_candidates_by_participant_overlap``. Each item's
            ``"node"`` must have a ``.label`` attribute.
        new_label: the new proposal's label.

    Returns:
        Filtered scored list, same shape.
    """
    new_norm = (new_label or "").strip().casefold()
    if not new_norm:
        return scored  # no new label to compare against — pass through
    return [
        s for s in scored
        if (getattr(s["node"], "label", "") or "").strip().casefold() == new_norm
    ]


def _filter_candidates_by_min_jaccard(
    scored: list,
    threshold: float = _MIN_JACCARD_FOR_LLM_CONSIDERATION,
) -> list:
    """Decide-not-a-match for State/Event/Goal candidates whose participant-
    overlap Jaccard is below ``threshold``.

    Why: the candidate-search pre-filter (participant overlap > 0) is too
    permissive. Sharing a single hub-y participant like "Jukka's children"
    is enough to put a candidate in the LLM's queue, but a single hub
    overlap is near-zero evidence of being the same instance — it just
    means both events involve some family member. A meaningful merge
    signal needs MAJORITY overlap (the participant fingerprints have to
    actually look the same).

    The 2026-05-03 Performance over-merge had jaccard 1/6 ≈ 0.17 — a
    single hub overlap with a totally different specific participant
    ("Drowsy Chaperone") on the new side and four unrelated participants
    ("South Lake Middle School", "Annika", "Jorma", "Seija" from
    Beetlejuice context) on the existing side. A 0.5 threshold drops it.

    Args:
        scored: candidate list as returned by
            ``_score_candidates_by_participant_overlap``.
        threshold: minimum jaccard (0..1) required to keep the candidate.
            Default ``_MIN_JACCARD_FOR_LLM_CONSIDERATION``.

    Returns:
        Filtered scored list, same shape.
    """
    return [s for s in scored if s.get("jaccard", 0.0) >= threshold]


_YEAR_RE = __import__("re").compile(r"\b(?:19|20|21)\d{2}\b")


def _extract_years_from_prose(*texts: str) -> set:
    """Pull 4-digit years (1900-2199) out of one or more prose strings.
    Used by _filter_candidates_by_time_frame to compare partial date info
    when structured start/end dates are missing."""
    out: set = set()
    for t in texts:
        if not t:
            continue
        for m in _YEAR_RE.findall(str(t)):
            try:
                out.add(int(m))
            except ValueError:
                continue
    return out


def _filter_candidates_by_time_frame(
    scored: list,
    new_valid_from=None,
    new_valid_to=None,
    new_start_prose: str = "",
    new_end_prose: str = "",
    event_tolerance_days: int = _EVENT_DATE_TOLERANCE_DAYS,
) -> list:
    """Decide-not-a-match for State/Event/Goal candidates whose time-frame
    contradicts the new proposal. Universal — applies to all node types
    that have time-frame fields. Replaces the old Event-only date filter.

    A candidate is dismissed when ANY of these conditions is true:

      - Both sides have fully-known dates AND windows differ by more than
        ``event_tolerance_days`` (preserves the original Event behavior).
      - Sequential — the candidate ENDED before the new proposal STARTED
        (cand.end_date strictly before new.valid_from), or vice versa
        (new.valid_to strictly before cand.start_date). No tolerance —
        if a state explicitly ended before another started, they are
        sequential instances by definition.
      - Year mismatch via prose — both sides have prose date info, both
        contain at least one 4-digit year, and the year sets are disjoint.

    A candidate is KEPT when:
      - Both sides are fully dateless (no evidence either way).
      - Either side is dateless and the other side has dates (legitimate
        evolution flow: vague mention later refined).
      - Open-ended overlapping (cand.start set, end None; new.valid_from
        within cand's window).

    Args:
        scored: candidate list as returned by
            ``_score_candidates_by_participant_overlap``. Each item's
            ``"node"`` should have ``.start_date`` / ``.end_date`` /
            ``.start_date_prose`` / ``.end_date_prose`` (any of which
            may be None / "").
        new_valid_from / new_valid_to: structured datetimes for the new
            proposal, or None.
        new_start_prose / new_end_prose: partial date prose for the new
            proposal, or "".
        event_tolerance_days: slack for the both-fully-dated case.

    Returns:
        Filtered scored list, same shape.
    """
    new_years = _extract_years_from_prose(new_start_prose, new_end_prose)
    kept: list = []
    for s in scored:
        node = s["node"]
        cs = getattr(node, "start_date", None)
        ce = getattr(node, "end_date", None)
        cstart_prose = getattr(node, "start_date_prose", "") or ""
        cend_prose = getattr(node, "end_date_prose", "") or ""

        # Sequential check 1: candidate ended before new proposal started.
        if ce is not None and new_valid_from is not None:
            try:
                if ce.date() < new_valid_from.date():
                    continue  # not a match
            except Exception:
                pass

        # Sequential check 2: new proposal ended before candidate started.
        if new_valid_to is not None and cs is not None:
            try:
                if new_valid_to.date() < cs.date():
                    continue
            except Exception:
                pass

        # Year-mismatch via prose. Only fires when both sides have year
        # info AND the year sets are disjoint.
        cand_years = _extract_years_from_prose(cstart_prose, cend_prose)
        if cand_years and new_years and cand_years.isdisjoint(new_years):
            continue

        # NOTE: an Event-specific start-date distance tolerance lives in
        # the separate _filter_event_candidates_by_date helper, applied
        # by _prepare_proposal_plan for Event-typed proposals. We do NOT
        # apply it here — it would wrongly drop legitimate open-ended
        # State observations (Residence since 2020, new observation in
        # 2023). Sequential is the only universal time-rejection signal.

        kept.append(s)
    return kept


def _filter_event_candidates_by_date(
    scored: list,
    new_valid_from,
    tolerance_days: int = _EVENT_DATE_TOLERANCE_DAYS,
) -> list:
    """Decide-not-a-match for Event candidates whose start_date differs
    from the new proposal's valid_from by more than tolerance_days.

    Dateless candidates are KEPT (the LLM still gets to evaluate them).
    Most State/Event nodes don't have dates initially — dates get refined
    over time as more is learned. A vague dateless mention being later
    refined by a dated mention is a legitimate evolution flow; both should
    resolve to the same node. So "no date on the candidate" is not
    evidence of non-match — only "both sides have known time-frames AND
    they don't overlap" is.

    Hub-only over-merge cases (e.g. the 2026-05-03 Performance hub) are
    caught at the participant-overlap-strength layer (Jaccard threshold
    or hub-weighted overlap), not here.

    Args:
        scored: candidate list as returned by
            _score_candidates_by_participant_overlap. Each item has a
            "node" with a `.start_date` attribute (datetime or None).
        new_valid_from: datetime or None. If None, no filtering applied
            (a dateless new proposal can match anything).
        tolerance_days: days of slack on either side of new_valid_from.

    Returns:
        Filtered scored list, same shape.
    """
    if new_valid_from is None:
        return scored
    kept: list = []
    for s in scored:
        cs = s["node"].start_date
        if cs is None:
            # Dateless candidate — KEEP. Time-frame unknown on this side
            # is not evidence of non-match; the LLM (or a later filter
            # like Jaccard threshold) decides using participants, sentence,
            # window_text.
            kept.append(s)
            continue
        try:
            delta = abs((cs.date() - new_valid_from.date()).days)
        except Exception:
            kept.append(s)
            continue
        if delta <= tolerance_days:
            kept.append(s)
    return kept


def _first_window_id_for_node(session, node_id: str) -> Optional[str]:
    """Return the earliest window_id recorded on kg_node_evidence for this node,
    or None if the node has no evidence rows."""
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT window_id FROM kg_node_evidence "
                "WHERE node_id = :nid AND window_id IS NOT NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"nid": node_id},
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _first_window_id_for_proposal(session, proposal_id: str) -> Optional[str]:
    """Earliest window_id on claim_proposal_evidence for the given proposal.
    Used as the proposal's ``source window`` for same-window merge logic.
    Orders by created_at (always set) rather than observed_at (nullable)."""
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT window_id FROM claim_proposal_evidence "
                "WHERE proposal_id = :pid AND window_id IS NOT NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"pid": proposal_id},
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _node_window_text(session, node_id: str, max_chars: int = 600) -> Optional[str]:
    """Source text from kg_node_evidence's first window for this node.
    Compact pipe-delimited message snippet (already in the table) — gives
    the merger the actual chat context the node was extracted from, which
    is what disambiguates same-label states whose participants overlap by
    chance. Returns None if no evidence text exists.
    """
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT source_text FROM kg_node_evidence "
                "WHERE node_id = :nid AND source_text IS NOT NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"nid": node_id},
        ).fetchone()
        if not row or not row[0]:
            return None
        txt = str(row[0])
        return txt if len(txt) <= max_chars else txt[:max_chars] + "…"
    except Exception:
        return None


def _proposal_window_text(session, proposal_id: str, max_chars: int = 600) -> Optional[str]:
    """raw_text from claim_proposal_evidence's first window for this
    proposal — the new-side counterpart of _node_window_text."""
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT raw_text FROM claim_proposal_evidence "
                "WHERE proposal_id = :pid AND raw_text IS NOT NULL "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"pid": proposal_id},
        ).fetchone()
        if not row or not row[0]:
            return None
        txt = str(row[0])
        return txt if len(txt) <= max_chars else txt[:max_chars] + "…"
    except Exception:
        return None


def _participant_labels(session, node_id: str, limit: int = 6) -> List[str]:
    """Labels of entity-like neighbors — the concrete participant names
    the merger needs to reason about identity."""
    rows = (
        session.query(Edge.source_id, Edge.target_id)
        .filter(or_(Edge.source_id == node_id, Edge.target_id == node_id))
        .all()
    )
    other_ids: Set[str] = set()
    for src, tgt in rows:
        o = tgt if src == node_id else src
        if o and o != node_id:
            other_ids.add(o)
    if not other_ids:
        return []
    ents = (
        session.query(Node.id, Node.label)
        .filter(Node.id.in_(other_ids))
        .filter(Node.node_type.in_(list(ENTITY_LIKE_TYPES)))
        .all()
    )
    labels = [lbl or "(unlabeled)" for _id, lbl in ents[:limit]]
    return labels


def _window_end_ts(session, window_id: Optional[str]) -> Optional[str]:
    """End timestamp of a conversation window, for LLM time reasoning."""
    if not window_id:
        return None
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text("SELECT end_timestamp FROM kg_window WHERE id = :w"),
            {"w": window_id},
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return None


def _call_node_merger_for_state_match(
    new_node_ctx: Dict[str, Any],
    candidate_payload: List[Dict[str, Any]],
) -> Optional[str]:
    """Session-free node_merger call for State/Event merge decisions.

    Both ``new_node_ctx`` and ``candidate_payload`` are pre-built by
    ``_prepare_proposal_plan`` from a closed read session. This function
    only makes the LLM call — no DB access — so it can run safely outside
    any write transaction.

    Returns the matched candidate's KG ``node_id`` (a string), or None if
    no merge.
    """
    import json
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
        from app.assistant.utils.pydantic_classes import Message
    except Exception as exc:
        logger.warning("[promoter] node_merger unavailable: %s", exc)
        return None

    agent = DI.agent_factory.create_agent("knowledge_graph_add::node_merger")
    if agent is None:
        logger.warning("[promoter] could not create node_merger agent")
        return None

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="proposal_promoter",
    )
    agent_input = {
        "new_node_context": json.dumps(new_node_ctx, ensure_ascii=True, indent=2),
        "existing_node_candidates": json.dumps(candidate_payload, ensure_ascii=True, indent=2),
    }
    try:
        resp = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = resp.data if resp and hasattr(resp, "data") else {}
    except Exception as exc:
        logger.warning("[promoter] node_merger call failed for state %r: %s",
                       new_node_ctx.get("label"), exc)
        return None

    if not bool(data.get("merge_nodes")):
        return None
    merged_id = str(data.get("merged_node_id") or "").strip()
    if not merged_id:
        return None
    cand_ids = {c.get("node_id") for c in candidate_payload}
    if merged_id not in cand_ids:
        logger.warning(
            "[promoter] node_merger returned id %s not in candidate set",
            merged_id,
        )
        return None
    logger.info(
        "[promoter] LLM merged state %r → %s: %s",
        new_node_ctx.get("label"), merged_id[:8],
        (data.get("reasoning") or "")[:120],
    )
    return merged_id


def _get_participant_kg_ids(session, node_id: str) -> Set[str]:
    """Return the kg_node_metadata.id of every Entity-like node connected
    to this node via any relationship edge (in either direction).

    Used to compute the participant fingerprint of a State/Event node.
    """
    rows = (
        session.query(Edge.source_id, Edge.target_id)
        .filter(or_(Edge.source_id == node_id, Edge.target_id == node_id))
        .all()
    )
    others: Set[str] = set()
    for src, tgt in rows:
        other = tgt if src == node_id else src
        if other and other != node_id:
            others.add(other)
    if not others:
        return set()

    # Only keep entity-like nodes (filter out other State/Event neighbors).
    entities = (
        session.query(Node.id)
        .filter(Node.id.in_(others))
        .filter(Node.node_type.in_(list(ENTITY_LIKE_TYPES)))
        .all()
    )
    return {r[0] for r in entities}


# ---------------------------------------------------------------------------
# Node / edge creation
# ---------------------------------------------------------------------------

def _create_kg_node_from_proposal(
    session, proposal_node: ClaimProposalNode, proposal_id: str,
    *,
    ttl: Optional[Dict[str, Any]] = None,
    canonical_sentence: Optional[str] = None,
) -> Node:
    """Create a fresh kg_node_metadata row from this proposal_node.

    ``ttl`` and ``canonical_sentence`` MUST be precomputed by the caller via
    ``_prepare_proposal_plan`` before opening any write transaction. They
    are LLM-derived (state_ttl_estimator, fact_canonicalizer) and computing
    them while the writer slot is held cascades "database is locked" errors
    across every other writer in the process.

    For State/Event nodes, ``ttl`` lands in ``attributes.ttl`` so the
    nightly decay job can auto-close the era when it expires. For
    State/Event/Goal nodes, ``canonical_sentence`` becomes
    ``Node.original_sentence``; entity-like nodes (Entity/Concept/Goal/
    Property) take the raw extractor sentence verbatim.
    """
    attrs = dict(proposal_node.attributes_json or {}) if isinstance(proposal_node.attributes_json, dict) else {}
    if ttl is not None and proposal_node.node_type in RELATIONSHIP_LIKE_TYPES:
        attrs["ttl"] = ttl

    sentence_for_node = (proposal_node.sentence or "") if hasattr(proposal_node, "sentence") else ""
    if proposal_node.node_type in {"State", "Event", "Goal"} and canonical_sentence:
        sentence_for_node = canonical_sentence

    new = Node(
        label=proposal_node.label,
        node_type=proposal_node.node_type,
        # Present-tense canonical for State/Event/Goal (via fact_canonicalizer);
        # raw extractor sentence for Entity/Concept/Property. Verbatim source
        # is preserved in evidence + window_id, not on the node.
        original_sentence=sentence_for_node,
        # Description is intentionally LEFT BLANK at promotion time. A single
        # observation isn't enough to write a meaningful description; that's
        # the entity_card pipeline's job once enough evidence accumulates.
        # The card pipeline writes back to node.description when the card is
        # generated. The extractor's draft is preserved on the proposal row
        # (description_draft) for diagnostics; it just isn't promoted.
        description="",
        aliases=[],
        category=proposal_node.category,
        attributes=attrs,
        start_date=proposal_node.valid_from,
        end_date=proposal_node.valid_to,
        start_date_confidence=proposal_node.start_date_confidence,
        end_date_confidence=proposal_node.end_date_confidence,
        start_date_prose=proposal_node.valid_from_prose,
        end_date_prose=proposal_node.valid_to_prose,
        source="proposal_promoter",
        created_from_proposal_id=proposal_id,
    )
    session.add(new)
    session.flush()
    return new


def _estimate_state_ttl(
    proposal_node: ClaimProposalNode,
    *,
    originating_sentence: Optional[str] = None,
    participant_labels: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Ask state_ttl_estimator agent for a duration estimate. Returns a
    dict suitable for stashing in the node's attributes, or None if the
    agent can't be reached (fail silent — the decay job will treat
    TTL-less states as durable)."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
        from app.assistant.utils.pydantic_classes import Message
    except Exception as exc:
        logger.warning("[promoter] TTL estimator unavailable: %s", exc)
        return None

    agent = DI.agent_factory.create_agent("knowledge_graph_add::state_ttl_estimator")
    if agent is None:
        logger.warning("[promoter] could not create state_ttl_estimator agent")
        return None

    participants_str = ", ".join(participant_labels) if participant_labels else ""
    agent_input = {
        "state_label": proposal_node.label or "",
        "state_node_type": proposal_node.node_type or "State",
        "description": proposal_node.description_draft or "",
        "participants": participants_str,
        "valid_from": proposal_node.valid_from.isoformat() if proposal_node.valid_from else "",
        "originating_sentence": originating_sentence or "",
    }

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="proposal_promoter",
    )
    try:
        res = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = res.data if res and hasattr(res, "data") else {}
    except Exception as exc:
        logger.warning(
            "[promoter] TTL estimator failed for state %r: %s",
            proposal_node.label, exc,
        )
        return None

    if not isinstance(data, dict) or not data.get("duration_class"):
        return None
    days = data.get("estimated_duration_days")
    return {
        "duration_class": data.get("duration_class"),
        "estimated_duration_days": int(days) if isinstance(days, (int, float)) else None,
        "confidence": float(data.get("confidence") or 0.0),
        "reasoning": str(data.get("reasoning") or "")[:500],
        "estimated_at": datetime.utcnow().isoformat(),
    }


def _refresh_on_reobservation(node: Node, proposal: ClaimProposal) -> None:
    """Bump the matched node's observation-tracking attributes.

    Called on ``matched_existing`` in the promoter. Without this the decay
    step can't tell "re-observed last week" from "nobody's mentioned this
    in 6 months" — the ``updated_at`` column doesn't distinguish.

    Sets / updates:
      - ``attributes.last_observed`` — always overwrite with this proposal's
        observation timestamp (decay reads this).
      - ``attributes.first_observed`` — only set if missing (back-compat for
        legacy nodes pre-attribute-tracking).
      - ``attributes.observation_count`` — increment.
      - ``attributes.confidence`` — gentle bump (+0.05, capped at 1.0) but
        only if already set; don't invent a confidence from nothing.
    """
    observed = proposal.last_observed_at or proposal.first_observed_at
    if observed is None:
        return
    iso = observed.isoformat() if hasattr(observed, "isoformat") else str(observed)
    attrs = dict(node.attributes or {}) if isinstance(node.attributes, dict) else {}
    attrs["last_observed"] = iso
    attrs.setdefault("first_observed", iso)
    attrs["observation_count"] = int(attrs.get("observation_count") or 1) + 1
    if "confidence" in attrs:
        try:
            attrs["confidence"] = min(1.0, float(attrs["confidence"]) + 0.05)
        except (TypeError, ValueError):
            pass
    node.attributes = attrs


def _canonicalize_sentence(
    proposal_node: ClaimProposalNode,
    *,
    participant_labels: Optional[List[str]] = None,
) -> Optional[str]:
    """Ask fact_canonicalizer agent to rewrite the extractor sentence into
    its present-tense canonical form. The result becomes ``Node.original_sentence``
    on the new KG node so the validity dates have a coherent partner — the
    sentence states the proposition and the dates bound when it's true.

    Returns None on failure; caller falls back to the raw extractor sentence.
    Verbatim source remains recoverable via window_id + claim_proposal_evidence.
    """
    if not getattr(proposal_node, "sentence", None):
        return None  # nothing to canonicalize

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
        from app.assistant.utils.pydantic_classes import Message
    except Exception as exc:
        logger.warning("[promoter] fact_canonicalizer unavailable: %s", exc)
        return None

    agent = DI.agent_factory.create_agent("knowledge_graph_add::fact_canonicalizer")
    if agent is None:
        logger.warning("[promoter] could not create fact_canonicalizer agent")
        return None

    agent_input: Dict[str, Any] = {
        "extractor_sentence": proposal_node.sentence,
        "node_label": proposal_node.label or "",
        "node_type": proposal_node.node_type or "State",
    }
    if participant_labels:
        agent_input["participants"] = ", ".join(participant_labels)
    if proposal_node.valid_from:
        agent_input["start_date"] = proposal_node.valid_from.isoformat()
    if proposal_node.valid_to:
        agent_input["end_date"] = proposal_node.valid_to.isoformat()
    attrs = proposal_node.attributes_json or {}
    if isinstance(attrs, dict) and attrs.get("valid_during"):
        agent_input["valid_during"] = attrs["valid_during"]

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="proposal_promoter",
    )
    try:
        res = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = res.data if res and hasattr(res, "data") else {}
    except Exception as exc:
        logger.warning(
            "[promoter] canonicalizer failed for %r: %s",
            proposal_node.label, exc,
        )
        return None

    if not isinstance(data, dict):
        return None
    canonical = data.get("canonical_sentence")
    if not isinstance(canonical, str) or not canonical.strip():
        return None
    return canonical.strip()


def _earliest_proposal_evidence(session, proposal_id: str):
    """Pick the earliest claim_proposal_evidence row for this proposal.

    Used as the observation source for KG node/edge evidence rows the
    promoter writes — claim_proposal_evidence carries the canonical
    (window_id, unified_log_id, raw_text, observed_at, room_id, speaker_*)
    snapshot for each proposal.
    """
    from app.assistant.database.claim_proposals import ClaimProposalEvidence
    return (
        session.query(ClaimProposalEvidence)
        .filter(ClaimProposalEvidence.proposal_id == proposal_id)
        .order_by(ClaimProposalEvidence.observed_at.asc())
        .first()
    )


def _write_node_evidence(
    session,
    *,
    node_id: str,
    proposal: ClaimProposal,
    proposal_node: ClaimProposalNode,
    merge_action: str,
) -> None:
    """Append one kg_node_evidence row for a node observation.

    merge_action conventions match the existing KGNodeEvidence model
    docstring: "created" | "confirmed" | "updated". The promoter uses
    "created" when this proposal materialized a fresh kg_node row, and
    "confirmed" when it matched an existing one (reinforcement).

    Provenance fields are sourced from claim_proposal_evidence so the
    chain `kg_node_evidence → unified_log_id` resolves to the original
    chat message. Without this writer the entire post-rebuild cohort
    has empty evidence and the kg_node_viewer + node_merger LLM cannot
    see source context.
    """
    try:
        from app.assistant.database.kg_chat_projection import KGNodeEvidence
    except Exception as exc:
        logger.warning("[promoter] node evidence cascade unavailable: %s", exc)
        return

    ev = _earliest_proposal_evidence(session, proposal.id)
    session.add(KGNodeEvidence(
        node_id=node_id,
        source_table="unified_log_2026" if (ev and ev.unified_log_id) else None,
        source_id=(ev.unified_log_id if ev else None),
        source_text=(ev.raw_text if ev else None),
        derived_sentence=(proposal_node.sentence or None),
        message_timestamp=(ev.observed_at if ev else None),
        window_id=(ev.window_id if ev else None),
        merge_action=merge_action,
    ))


def _write_edge_evidence(
    session,
    *,
    edge_id: str,
    proposal: ClaimProposal,
    proposal_edge: ClaimProposalEdge,
    merge_action: str,
) -> None:
    """Append one kg_edge_evidence row for an edge observation.

    Replaces the previous _write_edge_evidence_for_reinforcement which
    was only called from the matched-existing path; the create path was
    silently skipping evidence too. merge_action is now a parameter so
    the same writer covers both: "created" for fresh edges, "confirmed"
    for reinforcement of an existing edge.
    """
    try:
        from app.assistant.database.kg_chat_projection import KGEdgeEvidence
    except Exception as exc:
        logger.warning("[promoter] edge evidence cascade unavailable: %s", exc)
        return

    ev = _earliest_proposal_evidence(session, proposal.id)
    session.add(KGEdgeEvidence(
        edge_id=edge_id,
        source_table="unified_log_2026" if (ev and ev.unified_log_id) else None,
        source_id=(ev.unified_log_id if ev else None),
        source_text=(ev.raw_text if ev else None),
        derived_sentence=proposal_edge.sentence,
        message_timestamp=(ev.observed_at if ev else None),
        window_id=(ev.window_id if ev else None),
        merge_action=merge_action,
    ))


def _existing_kg_edge(session, src_id: str, tgt_id: str, predicate: str) -> Optional[Edge]:
    return (
        session.query(Edge)
        .filter(
            Edge.source_id == src_id,
            Edge.target_id == tgt_id,
            Edge.relationship_type == predicate,
        )
        .first()
    )


def _is_durable_conflict(
    session, src_id: str, predicate: str, incoming_tgt_id: str,
) -> Optional[Edge]:
    """Does a durable-unique predicate already exist from src to a
    different target? (e.g., spouse can only have one target per source.)
    Returns the conflicting edge if so.

    Only invoked for predicates where a single-target invariant is
    expected (spouse, lives_at, works_at, etc.). For promiscuous
    predicates (participant, likes, topic), returns None.
    """
    SINGLE_TARGET_PREDICATES = {
        # Marriage is single-target by definition (polygamy not supported).
        "is_spouse_in", "is_married", "married_to",
        # Primary employer — debatable for contractors but usually
        # single-target in a biographical graph.
        "works_for", "employed_by",
        # Hard biological facts — single-target.
        "born_in",           # one birthplace
        "has_birthday",      # one date of birth
        "has_nationality",   # dual citizenship is rare; flag if multiple show up
        # Broader semantic contradictions (conflicting beliefs, conflicting
        # locations, habits) need LLM judgment — deliberately NOT covered
        # here. A perplexity-check agent is the right home for those.
    }
    if predicate not in SINGLE_TARGET_PREDICATES:
        return None
    hit = (
        session.query(Edge)
        .filter(Edge.source_id == src_id, Edge.relationship_type == predicate)
        .first()
    )
    if hit is None:
        return None
    if hit.target_id == incoming_tgt_id:
        return None
    return hit


# ---------------------------------------------------------------------------
# Decision container
# ---------------------------------------------------------------------------

class _NodeOutcome:
    __slots__ = ("pnode_id", "action", "resolved_node_id", "reason")
    def __init__(self, pnode_id, action, resolved_node_id=None, reason=""):
        self.pnode_id = pnode_id
        self.action = action  # matched_existing | created_new | skipped_locked | held_needs_existing
        self.resolved_node_id = resolved_node_id
        self.reason = reason


class _EdgeOutcome:
    __slots__ = ("pedge_id", "action", "resolved_edge_id", "reason")
    def __init__(self, pedge_id, action, resolved_edge_id=None, reason=""):
        self.pedge_id = pedge_id
        self.action = action  # created_new | matched_existing | skipped_conflict | skipped_locked
        self.resolved_edge_id = resolved_edge_id
        self.reason = reason


class _ProposalDecision:
    __slots__ = ("proposal_id", "final_status", "node_outcomes", "edge_outcomes", "error")
    def __init__(self, proposal_id):
        self.proposal_id = proposal_id
        self.final_status = "pending"
        self.node_outcomes: List[_NodeOutcome] = []
        self.edge_outcomes: List[_EdgeOutcome] = []
        self.error: Optional[str] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "final_status": self.final_status,
            "nodes": [(o.action, o.reason) for o in self.node_outcomes],
            "edges": [(o.action, o.reason) for o in self.edge_outcomes],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Prepared plan — built without holding any write lock
# ---------------------------------------------------------------------------
#
# The promoter runs in three phases per proposal:
#
#   Phase 1 (READ): open a read session, snapshot the proposal + nodes +
#     edges + candidate context for any state/event nodes. Close the
#     session before any LLM call. Reads do not block writers under WAL.
#
#   Phase 2 (LLM): with no session held, call the three LLM agents that
#     drive the decision: state_ttl_estimator, fact_canonicalizer, and
#     node_merger (only when there are candidates to consider). The
#     results are stored in a `_PromoterPlan` keyed by proposal-node id.
#
#   Phase 3 (APPLY): open a SHORT db_manager.transaction() and call
#     `_evaluate_and_apply(session, proposal, plan, commit=...)`. This
#     phase makes ZERO LLM calls; every decision is read from the plan.
#     The writer slot is held only for the deterministic SQL work, which
#     finishes in milliseconds.
#
# This split is the documented pattern in db_manager.py: do the LLM work
# without a session, then a single fast transaction to apply. Holding the
# SQLite write lock through an LLM call cascades "database is locked"
# errors across every other writer in the process (root cause of the
# 2026-05-02 cascade — see audit memo + commit message).


@dataclass
class _PreparedNode:
    """Per-proposal-node decision computed by `_prepare_proposal_plan`."""
    pn_id: str
    pn_label: str
    pn_node_type: str
    decision: str  # "match" | "create"
    matched_node_id: Optional[str] = None
    # Populated only for "create" decisions on RELATIONSHIP_LIKE_TYPES.
    ttl: Optional[Dict[str, Any]] = None
    # Populated only for "create" decisions on State/Event/Goal.
    canonical_sentence: Optional[str] = None


@dataclass
class _PromoterPlan:
    """All LLM-derived per-proposal data the apply phase needs."""
    proposal_id: str
    placeholder_labels: List[str] = field(default_factory=list)
    nodes: Dict[str, _PreparedNode] = field(default_factory=dict)


def _snapshot_state_candidate(session, cand: Node) -> Dict[str, Any]:
    """Plain-dict snapshot of a state/event candidate for the LLM merger
    call — captured while the read session is still open so the LLM call
    that follows can run with no session at all.
    """
    cand_attrs = cand.attributes if isinstance(cand.attributes, dict) else {}
    cand_ttl = cand_attrs.get("ttl") if isinstance(cand_attrs, dict) else None
    cand_window = _first_window_id_for_node(session, cand.id)
    return {
        "node_id": cand.id,
        "label": cand.label,
        "node_type": cand.node_type,
        "category": cand.category,
        "description": (cand.description or "")[:500],
        "original_sentence": cand.original_sentence,
        "start_date": cand.start_date.isoformat() if cand.start_date else None,
        "end_date": cand.end_date.isoformat() if cand.end_date else None,
        "start_date_prose": cand.start_date_prose,
        "end_date_prose": cand.end_date_prose,
        "first_observed": cand_attrs.get("first_observed") if isinstance(cand_attrs, dict) else None,
        "ttl_duration_class": (cand_ttl.get("duration_class") if isinstance(cand_ttl, dict) else None),
        "source_window_id": cand_window,
        "source_window_end_ts": _window_end_ts(session, cand_window),
        # Compact chat-window snippet THIS candidate was extracted from.
        # Lets the merger see the actual context that produced it, instead
        # of guessing identity from label + truncated description.
        "source_window_text": _node_window_text(session, cand.id),
        "participant_labels": _participant_labels(session, cand.id),
    }


class _ProposalNodeStub:
    """Lightweight stand-in for ClaimProposalNode used when calling
    `_estimate_state_ttl` / `_canonicalize_sentence` from the prepare
    phase. We can't pass the real ORM instance because the read session
    has already closed — accessing it would trigger a lazy load.
    """
    __slots__ = (
        "label", "node_type", "category", "description_draft",
        "valid_from", "valid_to", "valid_from_prose", "valid_to_prose",
        "attributes_json", "sentence",
        "start_date_confidence", "end_date_confidence",
    )

    def __init__(self, snap: Dict[str, Any]):
        self.label = snap["label"]
        self.node_type = snap["node_type"]
        self.category = snap["category"]
        self.description_draft = snap["description_draft"]
        self.valid_from = snap["valid_from"]
        self.valid_to = snap["valid_to"]
        self.valid_from_prose = snap["valid_from_prose"]
        self.valid_to_prose = snap["valid_to_prose"]
        self.attributes_json = snap["attributes_json"]
        self.sentence = snap["sentence"]
        self.start_date_confidence = snap.get("start_date_confidence")
        self.end_date_confidence = snap.get("end_date_confidence")


def _prepare_proposal_plan(proposal_id: str) -> Optional[_PromoterPlan]:
    """Phase 1+2 for a single proposal: read everything we need, close the
    session, then run the LLM agents (TTL, canonicalize, node_merger).

    Returns a `_PromoterPlan` the apply phase consumes deterministically.
    Returns None if the proposal vanished between read and now.

    Crucially: NO SQLAlchemy session is held while any LLM call runs.
    """
    # ---- Phase 1: read snapshot ----
    pnode_snaps: Dict[str, Dict[str, Any]] = {}
    pedge_relations: List[Tuple[Optional[str], Optional[str]]] = []  # (source_pn_id, target_pn_id) per edge for state participants
    entity_resolutions: Dict[str, Optional[str]] = {}
    candidate_lists: Dict[str, List[Dict[str, Any]]] = {}
    representative_sentence: str = ""
    group_entity_labels: List[str] = []
    proposal_window_id: Optional[str] = None
    proposal_window_end_ts: Optional[str] = None
    placeholder_labels: List[str] = []

    with get_db_manager().read_session() as session:
        proposal = (
            session.query(ClaimProposal)
            .filter(ClaimProposal.id == proposal_id)
            .first()
        )
        if proposal is None:
            return None

        pnodes = (
            session.query(ClaimProposalNode)
            .filter(ClaimProposalNode.proposal_id == proposal_id)
            .all()
        )
        pedges = (
            session.query(ClaimProposalEdge)
            .filter(ClaimProposalEdge.proposal_id == proposal_id)
            .all()
        )

        representative_sentence = proposal.representative_sentence or ""
        group_entity_labels = [
            p.label for p in pnodes if p.node_type in ENTITY_LIKE_TYPES and p.label
        ]
        proposal_window_id = _first_window_id_for_proposal(session, proposal_id)
        proposal_window_end_ts = _window_end_ts(session, proposal_window_id)
        proposal_window_text = _proposal_window_text(session, proposal_id)

        placeholder_labels = [pn.label for pn in pnodes if _is_placeholder_label(pn.label)]
        if placeholder_labels:
            return _PromoterPlan(
                proposal_id=proposal_id,
                placeholder_labels=placeholder_labels,
                nodes={},
            )

        for pn in pnodes:
            pnode_snaps[pn.id] = {
                "id": pn.id,
                "label": pn.label,
                "node_type": pn.node_type,
                "category": pn.category,
                "description_draft": pn.description_draft,
                "sentence": getattr(pn, "sentence", None),
                "valid_from": pn.valid_from,
                "valid_to": pn.valid_to,
                "valid_from_prose": pn.valid_from_prose,
                "valid_to_prose": pn.valid_to_prose,
                "attributes_json": pn.attributes_json or {},
                "start_date_confidence": pn.start_date_confidence,
                "end_date_confidence": pn.end_date_confidence,
            }

        for pe in pedges:
            pedge_relations.append((pe.source_node_id, pe.target_node_id))

        # Entity-like resolution: deterministic match against existing KG.
        # Done in the read session so resolved_node_ids are available for
        # the relationship-like participant lookup below.
        # Two passes: first the true entity-likes (Entity, Concept, Goal),
        # then Property — so Property's subject lookup can use the
        # already-resolved entity ids.
        for pn in pnodes:
            if pn.node_type in ENTITY_LIKE_TYPES and pn.node_type != "Property":
                m = _resolve_entity_like(session, pn.label)
                entity_resolutions[pn.id] = m.id if m else None

        for pn in pnodes:
            if pn.node_type != "Property":
                continue
            # Subjects = entity-like neighbors of this Property in the
            # proposal's own edges. Resolve via entity_resolutions to get
            # KG ids the property might already be attached to.
            subject_kg_ids: Set[str] = set()
            for src_pn, tgt_pn in pedge_relations:
                if src_pn == pn.id:
                    other = tgt_pn
                elif tgt_pn == pn.id:
                    other = src_pn
                else:
                    continue
                resolved = entity_resolutions.get(other) if other else None
                if resolved:
                    subject_kg_ids.add(resolved)
            m = _resolve_property(session, pn.label, subject_kg_ids)
            entity_resolutions[pn.id] = m.id if m else None

        # Relationship-like candidates: gather everything node_merger
        # would need, snapshot it, so the LLM call below has no session.
        for pn in pnodes:
            if pn.node_type not in RELATIONSHIP_LIKE_TYPES:
                continue

            # Participant KG ids: walk this proposal's edges, look up the
            # other endpoint in entity_resolutions. Only entity-like
            # nodes that resolved against the KG count as participants.
            participant_uuids: Set[str] = set()
            for src_pn, tgt_pn in pedge_relations:
                if src_pn == pn.id:
                    other = tgt_pn
                elif tgt_pn == pn.id:
                    other = src_pn
                else:
                    continue
                if other and other in entity_resolutions:
                    resolved = entity_resolutions[other]
                    if resolved:
                        participant_uuids.add(resolved)

            if not participant_uuids:
                candidate_lists[pn.id] = []
                continue

            # Pre-filter: state/event nodes connected to any participant.
            connected_ids: Set[str] = set()
            for src, tgt in (
                session.query(Edge.source_id, Edge.target_id)
                .filter(or_(
                    Edge.source_id.in_(participant_uuids),
                    Edge.target_id.in_(participant_uuids),
                ))
                .all()
            ):
                if src in participant_uuids and tgt not in participant_uuids:
                    connected_ids.add(tgt)
                elif tgt in participant_uuids and src not in participant_uuids:
                    connected_ids.add(src)
            if not connected_ids:
                candidate_lists[pn.id] = []
                continue

            cands = (
                session.query(Node)
                .filter(Node.node_type == pn.node_type)
                .filter(Node.id.in_(connected_ids))
                .all()
            )
            if not cands:
                candidate_lists[pn.id] = []
                continue

            # Score by participant overlap (Jaccard) — extracted to a pure
            # function for unit-testability. See test_state_event_merge.py.
            cands_with_parts = [
                (cand, _get_participant_kg_ids(session, cand.id))
                for cand in cands
            ]
            scored = _score_candidates_by_participant_overlap(
                cands_with_parts, participant_uuids,
            )

            # Universal time-frame exclusion (sequential + year-mismatch).
            # Applies to State/Event/Goal alike. Identity States (Marriage,
            # Residence) survive because their windows overlap or one side
            # is dateless.
            scored = _filter_candidates_by_time_frame(
                scored,
                new_valid_from=pn.valid_from,
                new_valid_to=pn.valid_to,
                new_start_prose=getattr(pn, "valid_from_prose", "") or "",
                new_end_prose=getattr(pn, "valid_to_prose", "") or "",
            )

            # Event-specific start-date distance tolerance (legacy behavior).
            # Catches "two Events with start dates more than 7 days apart"
            # — the universal filter doesn't do this because it would
            # over-reject open-ended State observations.
            if (pn.node_type or "").lower() == "event":
                scored = _filter_event_candidates_by_date(scored, pn.valid_from)

            # Hub-only-overlap exclusion. The candidate query above is
            # generous (any non-zero participant overlap qualifies), which
            # makes single-hub-entity overlaps ("Jukka's children" alone)
            # noise-grade evidence. Require majority participant overlap
            # before sending to the LLM. See the 2026-05-03 Performance
            # over-merge investigation for the failure mode this prevents.
            scored = _filter_candidates_by_min_jaccard(scored)

            # Hub-weighted overlap exclusion (complements Min-Jaccard).
            # Sums inverse-degree weights of shared participants. A high
            # Jaccard composed only of hub entities still gets dropped
            # here because hub weights are tiny (1/100s); a low Jaccard
            # composed of one obscure entity passes because that entity's
            # weight is large (1/<10).
            if scored:
                shared_ids: set = set()
                for sc in scored:
                    shared_ids |= sc.get("intersection", set())
                if shared_ids:
                    deg_rows = (
                        session.query(Edge.source_id, Edge.target_id)
                        .filter(or_(
                            Edge.source_id.in_(list(shared_ids)),
                            Edge.target_id.in_(list(shared_ids)),
                        ))
                        .all()
                    )
                    entity_degrees: Dict[str, int] = {eid: 0 for eid in shared_ids}
                    for src, tgt in deg_rows:
                        if src in entity_degrees:
                            entity_degrees[src] += 1
                        if tgt in entity_degrees:
                            entity_degrees[tgt] += 1
                    scored = _filter_candidates_by_weighted_overlap(
                        scored, entity_degrees,
                    )

            # Label-mismatch exclusion. Labels are intentionally drawn from
            # a narrow standardized set, so SAME label is weak evidence and
            # still requires LLM, but DIFFERENT labels are strong evidence
            # of non-match — the standardization wouldn't put truly-related
            # instances in different buckets.
            scored = _filter_candidates_by_label_equality(scored, pn.label)

            # Cap LLM input — hub participants (Jukka) explode candidate count.
            candidate_lists[pn.id] = [
                _snapshot_state_candidate(session, s["node"]) for s in scored[:5]
            ]

    # ---- Read session closed. Phase 2: LLM calls, no session held. ----
    nodes: Dict[str, _PreparedNode] = {}

    # Entity-like nodes: decision is just match-or-create, no LLM.
    for pn_id, snap in pnode_snaps.items():
        if snap["node_type"] not in ENTITY_LIKE_TYPES:
            continue
        match_id = entity_resolutions.get(pn_id)
        nodes[pn_id] = _PreparedNode(
            pn_id=pn_id,
            pn_label=snap["label"] or "",
            pn_node_type=snap["node_type"],
            decision="match" if match_id else "create",
            matched_node_id=match_id,
        )

    # Relationship-like nodes: optionally call node_merger if there are
    # candidates, then either record the match or precompute TTL +
    # canonical sentence for the create path.
    for pn_id, snap in pnode_snaps.items():
        if snap["node_type"] not in RELATIONSHIP_LIKE_TYPES:
            continue

        candidates = candidate_lists.get(pn_id, [])
        matched_node_id: Optional[str] = None
        if candidates:
            attrs = snap["attributes_json"] if isinstance(snap["attributes_json"], dict) else {}
            proposal_ttl = attrs.get("ttl") if isinstance(attrs, dict) else None
            new_node_ctx = {
                "label": snap["label"],
                "node_type": snap["node_type"],
                "category": snap["category"],
                "description": snap["description_draft"],
                "original_sentence": snap["sentence"],
                "valid_from": snap["valid_from"].isoformat() if snap["valid_from"] else None,
                "valid_to": snap["valid_to"].isoformat() if snap["valid_to"] else None,
                "valid_from_prose": snap["valid_from_prose"],
                "valid_to_prose": snap["valid_to_prose"],
                "source_window_id": proposal_window_id,
                "source_window_end_ts": proposal_window_end_ts,
                # Chat-window snippet for the NEW node — paired with
                # `source_window_text` on each candidate so the merger
                # can compare actual context, not just labels.
                "source_window_text": proposal_window_text,
                "first_observed": attrs.get("first_observed") if isinstance(attrs, dict) else None,
                "ttl_duration_class": (proposal_ttl.get("duration_class") if isinstance(proposal_ttl, dict) else None),
            }
            matched_node_id = _call_node_merger_for_state_match(new_node_ctx, candidates)

        if matched_node_id:
            nodes[pn_id] = _PreparedNode(
                pn_id=pn_id,
                pn_label=snap["label"] or "",
                pn_node_type=snap["node_type"],
                decision="match",
                matched_node_id=matched_node_id,
            )
            continue

        # No match — going to create. Precompute the LLM-derived fields.
        stub = _ProposalNodeStub(snap)
        ttl: Optional[Dict[str, Any]] = None
        canonical_sentence: Optional[str] = None
        if snap["node_type"] in RELATIONSHIP_LIKE_TYPES:
            ttl = _estimate_state_ttl(
                stub,
                originating_sentence=representative_sentence,
                participant_labels=group_entity_labels,
            )
        if snap["node_type"] in {"State", "Event", "Goal"}:
            canonical_sentence = _canonicalize_sentence(
                stub, participant_labels=group_entity_labels,
            )

        nodes[pn_id] = _PreparedNode(
            pn_id=pn_id,
            pn_label=snap["label"] or "",
            pn_node_type=snap["node_type"],
            decision="create",
            ttl=ttl,
            canonical_sentence=canonical_sentence,
        )

    return _PromoterPlan(
        proposal_id=proposal_id,
        placeholder_labels=[],
        nodes=nodes,
    )


# ---------------------------------------------------------------------------
# Core: apply one proposal's prepared plan
# ---------------------------------------------------------------------------

def _evaluate_and_apply(
    session, proposal: ClaimProposal, plan: _PromoterPlan, *, commit: bool,
) -> _ProposalDecision:
    """Apply a precomputed `_PromoterPlan` against the KG.

    Caller MUST be inside a write transaction (use db_manager.transaction).
    This function makes ZERO LLM calls — every match/create decision is
    read from `plan`. The plan must have been built by
    `_prepare_proposal_plan` with no session held during its LLM calls.
    """
    dec = _ProposalDecision(proposal.id)

    if plan.placeholder_labels:
        dec.final_status = "contradicted"
        dec.error = f"placeholder labels present: {plan.placeholder_labels}"
        # Emit one outcome per pnode for symmetry with prior callers.
        pnodes_for_outcome = (
            session.query(ClaimProposalNode)
            .filter(ClaimProposalNode.proposal_id == proposal.id)
            .all()
        )
        for pn in pnodes_for_outcome:
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "skipped_locked",
                reason=f"group rejected (placeholder labels: {plan.placeholder_labels})",
            ))
        return dec

    pnodes = (
        session.query(ClaimProposalNode)
        .filter(ClaimProposalNode.proposal_id == proposal.id)
        .all()
    )
    pedges = (
        session.query(ClaimProposalEdge)
        .filter(ClaimProposalEdge.proposal_id == proposal.id)
        .all()
    )

    # ----- Nodes: entity-like first, then relationship-like.
    # The plan tells us match-or-create for each. We just write.

    # Phase 1a: entity-like.
    for pn in pnodes:
        if pn.node_type not in ENTITY_LIKE_TYPES:
            continue
        prepared = plan.nodes.get(pn.id)
        if prepared is None:
            # New pnode appeared between prepare and apply — rare, but
            # don't silently drop it; mark as skipped.
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "skipped_conflict", None,
                "no plan entry (proposal mutated between prepare and apply)",
            ))
            continue

        if prepared.decision == "match" and prepared.matched_node_id:
            pn.resolved_node_id = prepared.matched_node_id
            pn.resolution_action = "matched_existing"
            if commit:
                match_node = session.get(Node, prepared.matched_node_id)
                if match_node is not None:
                    _refresh_on_reobservation(match_node, proposal)
                _write_node_evidence(
                    session,
                    node_id=prepared.matched_node_id,
                    proposal=proposal,
                    proposal_node=pn,
                    merge_action="confirmed",
                )
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "matched_existing", prepared.matched_node_id,
                f"{pn.node_type} {pn.label!r} matched {prepared.matched_node_id[:8]}",
            ))
        else:
            if commit:
                new = _create_kg_node_from_proposal(session, pn, proposal.id)
                pn.resolved_node_id = new.id
                pn.resolution_action = "created_new"
                _write_node_evidence(
                    session,
                    node_id=new.id,
                    proposal=proposal,
                    proposal_node=pn,
                    merge_action="created",
                )
                dec.node_outcomes.append(_NodeOutcome(
                    pn.id, "created_new", new.id,
                    f"created {pn.node_type} {pn.label!r} as {new.id[:8]}",
                ))
            else:
                dec.node_outcomes.append(_NodeOutcome(
                    pn.id, "created_new", None,
                    f"(dry-run) would create {pn.node_type} {pn.label!r}",
                ))

    # Phase 1b: relationship-like.
    for pn in pnodes:
        if pn.node_type not in RELATIONSHIP_LIKE_TYPES:
            continue
        prepared = plan.nodes.get(pn.id)
        if prepared is None:
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "skipped_conflict", None,
                "no plan entry (proposal mutated between prepare and apply)",
            ))
            continue

        if prepared.decision == "match" and prepared.matched_node_id:
            pn.resolved_node_id = prepared.matched_node_id
            pn.resolution_action = "matched_existing"
            if commit:
                match_node = session.get(Node, prepared.matched_node_id)
                if match_node is not None:
                    _refresh_on_reobservation(match_node, proposal)
                _write_node_evidence(
                    session,
                    node_id=prepared.matched_node_id,
                    proposal=proposal,
                    proposal_node=pn,
                    merge_action="confirmed",
                )
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "matched_existing", prepared.matched_node_id,
                f"{pn.node_type} {pn.label!r} matched {prepared.matched_node_id[:8]} "
                f"via participants",
            ))
        else:
            if commit:
                new = _create_kg_node_from_proposal(
                    session, pn, proposal.id,
                    ttl=prepared.ttl,
                    canonical_sentence=prepared.canonical_sentence,
                )
                pn.resolved_node_id = new.id
                pn.resolution_action = "created_new"
                _write_node_evidence(
                    session,
                    node_id=new.id,
                    proposal=proposal,
                    proposal_node=pn,
                    merge_action="created",
                )
                ttl_blurb = ""
                if isinstance(new.attributes, dict) and "ttl" in new.attributes:
                    t = new.attributes["ttl"]
                    ttl_blurb = (
                        f" [ttl: {t.get('duration_class')}="
                        f"{t.get('estimated_duration_days')}d, "
                        f"conf={t.get('confidence'):.2f}]"
                    )
                dec.node_outcomes.append(_NodeOutcome(
                    pn.id, "created_new", new.id,
                    f"created {pn.node_type} {pn.label!r} as {new.id[:8]}{ttl_blurb}",
                ))
            else:
                dec.node_outcomes.append(_NodeOutcome(
                    pn.id, "created_new", None,
                    f"(dry-run) would create {pn.node_type} {pn.label!r}",
                ))

    # ----- Edges: create fresh or reinforce, respect locks + conflicts -----
    # Build quick lookup of resolved_node_id by proposal_node.id
    resolved_lookup: Dict[str, Optional[str]] = {pn.id: pn.resolved_node_id for pn in pnodes}

    # Pod URIs (datapod:<kind>:<id>) bypass proposal-node resolution because
    # they are already valid kg_node_metadata ids via kg_mirror — the pod
    # node is minted at PodStore.put() time. The fact_extractor + proposal
    # writer thread these URIs through verbatim; here the promoter accepts
    # them as already-resolved endpoints.
    from app.assistant.pod_store.pod_uri import POD_URI_RE

    def _resolve_endpoint(pn_id: str) -> Optional[str]:
        v = resolved_lookup.get(pn_id)
        if v is not None:
            return v
        if pn_id and POD_URI_RE.fullmatch(pn_id):
            return pn_id  # pod URI is its own kg_node_metadata id
        return None

    for pe in pedges:
        src_kg = _resolve_endpoint(pe.source_node_id)
        tgt_kg = _resolve_endpoint(pe.target_node_id)
        if src_kg is None or tgt_kg is None:
            # dry-run may leave new nodes without resolved_node_id — mark skipped
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "skipped_conflict", None,
                             "endpoint unresolved (new-node in dry-run)" if not commit else
                             "endpoint unresolved (unexpected in commit mode)")
            )
            continue

        existing = _existing_kg_edge(session, src_kg, tgt_kg, pe.predicate)
        if existing is not None:
            pe.resolved_edge_id = existing.id
            # Evidence cascade: record this reinforcement observation on
            # the KG edge's provenance log. The graph itself now knows the
            # edge was re-observed (not just our proposal layer).
            if commit:
                _write_edge_evidence(
                    session,
                    edge_id=existing.id,
                    proposal=proposal,
                    proposal_edge=pe,
                    merge_action="confirmed",
                )
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "matched_existing", existing.id,
                             f"edge exists {existing.id[:8]} — evidence appended")
            )
            continue

        # Check durable single-target conflict (e.g., second spouse).
        conflict = _is_durable_conflict(session, src_kg, pe.predicate, tgt_kg)
        if conflict is not None:
            src_node = session.get(Node, src_kg)
            if src_node and src_node.locked_by_user_at is not None:
                dec.edge_outcomes.append(
                    _EdgeOutcome(pe.id, "skipped_locked", None,
                                 f"locked {src_node.label!r} already has "
                                 f"{pe.predicate!r} edge to {conflict.target_id[:8]}")
                )
                dec.final_status = "contradicted"
                dec.error = f"lock conflict on {pe.predicate}"
                return dec
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "skipped_conflict", None,
                             f"existing {pe.predicate!r} edge to "
                             f"{conflict.target_id[:8]} conflicts with proposed {tgt_kg[:8]}")
            )
            dec.final_status = "contradicted"
            dec.error = f"conflict on {pe.predicate}"
            return dec

        # Green light to create.
        if commit:
            new_edge = Edge(
                source_id=src_kg,
                target_id=tgt_kg,
                relationship_type=pe.predicate,
                sentence=pe.sentence,
                window_id=None,  # proposal is window-level; edge record sits without
                source="proposal_promoter",
                created_from_proposal_id=proposal.id,
            )
            session.add(new_edge)
            session.flush()
            pe.resolved_edge_id = new_edge.id
            _write_edge_evidence(
                session,
                edge_id=new_edge.id,
                proposal=proposal,
                proposal_edge=pe,
                merge_action="created",
            )
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "created_new", new_edge.id,
                             f"new edge {new_edge.id[:8]} {pe.predicate}")
            )
        else:
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "created_new", None,
                             f"(dry-run) would create edge {pe.predicate}")
            )

    dec.final_status = "promoted"
    return dec


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_promoter(*, limit: int = 100, commit: bool = False) -> Dict[str, Any]:
    """Iterate pending proposals, evaluate each, optionally apply.

    Per proposal: read snapshot + LLM canonicalize (no write lock held)
    → short db_manager.transaction to apply. The writer slot is never
    held across an LLM call. See `_prepare_proposal_plan` for the
    contract; see db_manager.py for why this matters.
    """
    # Gate via the UI-controlled subsystem flag. See /dev/subsystems
    # to toggle. Default is enabled.
    from app.assistant.utils.subsystem_flags import is_subsystem_enabled
    if not is_subsystem_enabled("kg_proposal_promoter"):
        logger.info(
            "[promoter] skipped — subsystem flag kg_proposal_promoter is disabled."
        )
        return {
            "status": "skipped",
            "reason": "subsystem_disabled",
        }
    stats = {
        "evaluated": 0,
        "promoted": 0,
        "contradicted": 0,
        "errors": 0,
        "nodes_matched": 0,
        "nodes_created": 0,
        "edges_created": 0,
        "edges_matched": 0,
        "edges_skipped": 0,
    }
    samples: List[Dict[str, Any]] = []

    # Pull pending ids in a tiny read session — no write lock acquired.
    with get_db_manager().read_session() as session:
        pending_ids = [
            p.id for p in (
                session.query(ClaimProposal)
                .filter(ClaimProposal.status == "pending")
                .order_by(ClaimProposal.created_at.asc())
                .limit(limit)
                .all()
            )
        ]

    for pid in pending_ids:
        try:
            # Phase 1+2: read snapshot + LLM. NO write lock held.
            plan = _prepare_proposal_plan(pid)
            if plan is None:
                logger.warning("[promoter] proposal %s vanished before plan", pid)
                continue

            # Phase 3: short write transaction. NO LLM calls.
            with get_db_manager().transaction(op="promoter.apply") as session:
                p = (
                    session.query(ClaimProposal)
                    .filter(ClaimProposal.id == pid)
                    .first()
                )
                if p is None:
                    logger.warning("[promoter] proposal %s vanished before apply", pid)
                    continue
                if (p.status or "") != "pending":
                    # Someone else moved this proposal between prepare and
                    # apply. Skip — don't double-promote.
                    continue

                # Wrap the apply work in a SAVEPOINT so we can roll back
                # node/edge writes on a contradiction while still keeping
                # the outer status='contradicted' update.
                sp = session.begin_nested()
                try:
                    dec = _evaluate_and_apply(session, p, plan, commit=commit)
                except Exception as inner:
                    sp.rollback()
                    stats["errors"] += 1
                    logger.exception("[promoter] proposal %s threw during apply: %s", pid, inner)
                    continue

                stats["evaluated"] += 1
                for no in dec.node_outcomes:
                    if no.action == "matched_existing":
                        stats["nodes_matched"] += 1
                    elif no.action == "created_new":
                        stats["nodes_created"] += 1
                for eo in dec.edge_outcomes:
                    if eo.action == "created_new":
                        stats["edges_created"] += 1
                    elif eo.action == "matched_existing":
                        stats["edges_matched"] += 1
                    else:
                        stats["edges_skipped"] += 1

                if dec.final_status == "contradicted":
                    sp.rollback()
                    if commit:
                        p.status = "contradicted"
                        p.retraction_reason = dec.error or "conflict"
                    stats["contradicted"] += 1
                else:
                    if commit:
                        p.status = "promoted"
                    else:
                        # Dry-run: discard the pnode.resolved_node_id /
                        # resolution_action mutations the apply phase made
                        # on the live ORM rows. Without this rollback, the
                        # outer transaction commits them.
                        sp.rollback()
                    stats["promoted"] += 1

                if len(samples) < 10:
                    samples.append(dec.summary())
                # outer transaction commits at end of `with` — status update
                # only when commit=True. Dry-run commits an empty transaction.
        except Exception:
            stats["errors"] += 1
            logger.exception("[promoter] outer handler on proposal %s", pid)

    stats["_samples"] = samples
    return stats
