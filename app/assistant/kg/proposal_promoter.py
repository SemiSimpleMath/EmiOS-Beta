"""
Promoter for the redesigned claim_proposals (v2, group-based).

Walks pending proposals one at a time, evaluates each, and — when
``commit=True`` — applies the group to the KG atomically:

    For each proposal (in its own transaction):
        1. Resolve/create each node:
             - Entities / Concepts / Goals / Properties:
                 exact label match binds; alias match goes through
                 node_merger confirmation (aliases accrete from mention
                 labels and can be generic). No match → auto-create.
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
from datetime import datetime, timezone

from app.assistant.utils.time_utils import utc_now
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

_ENTITY_LIKE_MATCH_TYPES = ["Entity", "Concept", "Goal"]

# Cross-type binding allowlist (audit P2.3): which OTHER node_types a
# proposal of a given type may bind to by exact label. DELIBERATELY EMPTY —
# the old behavior (any entity-like type binds to any other: a Goal could
# silently bind to a same-label Concept) was an accident of the union
# filter, not a decision. Add pairs here intentionally if type-evolution
# tolerance is ever wanted.
_CROSS_TYPE_BIND_ALLOWLIST: dict[str, tuple[str, ...]] = {}

# Same-label binding to a well-connected person is the two-Alexes trap:
# different people share names, and a silent bind merges their lives. At
# or above this edge count the bind requires node_merger confirmation
# (audit P2.3 fix 3).
_PERSON_CONFIRM_MIN_EDGES = 5

# Semantic candidate tier (audit P2.3 fix 2) — same bar as the duplicate
# scan's tier 3.
_ENTITY_SEMANTIC_SIM_THRESHOLD = 0.88
_ENTITY_SEMANTIC_TOP_K = 8


def _resolve_entity_like(session, label: str, node_type: str) -> Optional[Tuple[Node, str]]:
    """Match an Entity/Concept/Goal node by canonical label or alias.

    Returns (node, tier) where tier is one of "disambiguation", "label",
    "mention_map", "alias" — or None on no match. Callers must treat the
    tiers differently: "disambiguation", "label", and "mention_map" are
    closed-form identity (the mention map records PRIOR node_merger
    confirmations and self-revokes on ambiguity). "alias" is NOT —
    aliases accrete from bound mention labels (P2.2(c) refinement), so a
    generic label like "House" can become an alias of one specific node
    and silently capture every future mention of anyone's house. Alias
    hits go through node_merger confirmation, and confirmed binds mint
    mention-map entries so the confirmation is paid once.

    Prefers locked + higher pagerank when multiple match.

    Hard-gated to the proposal's OWN node_type (+ explicit allowlist —
    currently empty). The pre-P2.3 filter used the {Entity, Concept, Goal}
    union, so a Goal proposal could silently bind to a same-label Concept.
    State/Event resolution goes through the separate relationship-like
    path (Jaccard participant overlap + LLM merger). Property goes
    through `_resolve_property` (subject-scoped match).

    Disambiguation precedence: if a Disambiguation node exists at this
    label, the referent is known-ambiguous — bind to the Disambiguation
    node itself (bypassing the type gate; it is the designated
    attachment point for ANY mention at that label), even over an exact
    label/alias hit. Edges land on it until the maintenance
    investigator re-points them to the true referent; once the
    ambiguity is resolved (alias on the real node + Disambiguation
    deleted), mentions bind normally again.
    """
    if not label:
        return None
    label_lower = label.lower().strip()
    if not label_lower:
        return None
    allowed_types = [node_type, *_CROSS_TYPE_BIND_ALLOWLIST.get(node_type, ())]

    # Disambiguation FIRST — known-ambiguous label beats default resolution.
    from app.assistant.kg.disambiguation import find_disambiguation
    dis = find_disambiguation(session, label)
    if dis is not None:
        return dis, "disambiguation"

    hit = (
        session.query(Node)
        .filter(func.lower(Node.label) == label_lower)
        .filter(Node.node_type.in_(allowed_types))
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )
    if hit:
        return hit, "label"

    # Mention map (identity phase 5): forms the node_merger has already
    # CONFIRMED, recorded durably, validated + self-revoking on every hit.
    # Closed-form because the confirmation already happened — this is the
    # learning step alias accretion tried to be, minus the capture bug.
    from app.assistant.kg.mention_map import lookup_mention
    mapped = lookup_mention(session, label, node_type)
    if mapped is not None:
        return mapped, "mention_map"

    like_pat = f'%"{label_lower}"%'
    hit = (
        session.query(Node)
        .filter(Node.node_type.in_(allowed_types))
        .filter(func.lower(func.coalesce(func.cast(Node.aliases, type_=__import__("sqlalchemy").String), "")).like(like_pat))
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )
    if hit:
        return hit, "alias"
    return None


def _needs_person_confirm(session, node: Node) -> bool:
    """True when an exact-label bind to this node is risky enough to need
    node_merger confirmation: a person-category node with substantial
    edges (audit P2.3 fix 3 — the two-Alexes collision). A wrong bind
    merges two people's lives; a duplicate is fixable by the dup scan."""
    if (node.category or "").strip().lower() != "person":
        return False
    edge_count = (
        session.query(Edge.id)
        .filter(or_(Edge.source_id == node.id, Edge.target_id == node.id))
        .count()
    )
    return edge_count >= _PERSON_CONFIRM_MIN_EDGES


def _semantic_entity_candidates(
    session, label: str, node_type: str, sentence: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Chroma-similarity candidates for an entity-like proposal that found
    no exact label/alias match (audit P2.3 fix 2): cosine >=
    _ENTITY_SEMANTIC_SIM_THRESHOLD against the label + context collections
    (the duplicate scan's tier-3 bar) PLUS the identity-sentence collection
    (definite descriptions — queried with the mention's own sentence when
    available; sentence-vs-sentence similarity is MiniLM's strength, and
    short generic labels are its weakness). Filtered to the proposal's own
    node_type, snapshotted for the post-session node_merger confirm.
    Catches "the user's mom" vs "the user's mother" before it mints a twin.

    Failure-isolated: chroma/embedder down → empty list (the proposal just
    creates, the dup scan remains the safety net)."""
    if not label or not label.strip():
        return []
    try:
        import numpy as np
        from app.assistant.embeddings.embedder import embed_text
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    except Exception as exc:
        logger.warning("[promoter] entity semantic tier unavailable: %s", exc)
        return []

    def _unit(text: str):
        v = np.array(embed_text(text), dtype=float)
        n = float(np.linalg.norm(v))
        return (v, n) if n > 0.0 else (None, 0.0)

    try:
        query_emb, query_n = _unit(label.strip())
        if query_emb is None:
            return []
        cm = get_chroma_manager()

        # Identity collection gets the mention sentence as its query when
        # the extractor provided one — that is the like-vs-like comparison.
        identity_emb, identity_n = (query_emb, query_n)
        if sentence and sentence.strip():
            emb, n = _unit(sentence.strip())
            if emb is not None:
                identity_emb, identity_n = emb, n

        # Top-K per collection, then exact cosine per candidate
        # (collection.query distances are L2 — same recompute pattern as
        # the scan's tier 3 and the label-relax filter).
        cand_ids: set[str] = set()
        for collection, q_emb, q_n in (
            (cm.node_collection, query_emb, query_n),
            (cm.node_context_collection, query_emb, query_n),
            (cm.node_identity_collection, identity_emb, identity_n),
        ):
            try:
                res = collection.query(
                    query_embeddings=[q_emb.tolist()],
                    n_results=_ENTITY_SEMANTIC_TOP_K,
                    include=["embeddings"],
                )
            except Exception:
                continue
            ids0 = (res.get("ids") or [[]])[0]
            embs0 = (res.get("embeddings") or [[]])[0]
            for cid, emb in zip(ids0, embs0):
                if emb is None or len(emb) == 0:
                    continue
                v = np.array(emb, dtype=float)
                n = float(np.linalg.norm(v))
                if n == 0.0:
                    continue
                if float(np.dot(q_emb, v) / (q_n * n)) >= _ENTITY_SEMANTIC_SIM_THRESHOLD:
                    cand_ids.add(str(cid))
        if not cand_ids:
            return []

        cands = (
            session.query(Node)
            .filter(Node.id.in_(list(cand_ids)))
            .filter(Node.node_type == node_type)
            .all()
        )
        return [_snapshot_state_candidate(session, c) for c in cands[:5]]
    except Exception as exc:
        logger.warning("[promoter] entity semantic tier failed for %r: %s", label, exc)
        return []


def _mint_disambiguation_on_label_collision(session, new_node) -> None:
    """After an entity-like create: if the new node's exact label now
    collides with another same-type node, mint a Disambiguation at that
    label.

    Reaching here means the resolver/node_merger saw the existing node
    and DECLINED the match (two-Alexes guard or merger verdict) — so
    the label genuinely has multiple referents. Without the marker,
    future mentions at this label would silently bind to whichever
    twin wins on pagerank. With it, they attach to the Disambiguation
    node until the investigator re-points them.
    """
    label = (new_node.label or "").strip()
    if not label:
        return
    collision = (
        session.query(Node.id)
        .filter(func.lower(Node.label) == label.lower())
        .filter(Node.node_type == new_node.node_type)
        .filter(Node.id != new_node.id)
        .first()
    )
    if collision is None:
        return
    from app.assistant.kg.disambiguation import create_disambiguation
    create_disambiguation(
        session,
        label=label,
        reason=(
            f"Minted by the promoter: a new {new_node.node_type} "
            f"({str(new_node.id)[:8]}) was created at a label already "
            f"held by {str(collision.id)[:8]} after the resolver "
            f"declined the match — the label has multiple referents."
        ),
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


_LABEL_SIMILARITY_THRESHOLD = 0.6   # cosine similarity, tuneable
# Calibrated 2026-05-12 against the Parenthood/Marriage/Sibling/Closeness
# candidate distribution. Reasoning: Jaccard upstream already requires
# majority participant overlap, so by the time this filter runs the
# candidates ALL share most participants. In that filtered pool,
# same-concept-different-label pairs typically embed at 0.5-0.9+ and
# different-concept candidates land at 0.4-0.6. 0.6 catches the merge-worthy
# same-concept variants ('Parenthood' ↔ 'Parent-Child Relationship',
# 'Marriage' ↔ 'Marriage Start') while still filtering most concept
# mismatches. LLM verification is the final arbiter for residual noise.


def _filter_candidates_by_label_or_similarity(
    scored: list,
    new_label: str,
    new_sentence: Optional[str] = None,
) -> list:
    """Keep candidates that match by label OR by sentence-embedding similarity.

    Relaxed 2026-05-12 from strict label-equality (formerly
    `_filter_candidates_by_label_equality`). The strict filter was rejecting
    legitimate same-concept-different-phrasing matches like
    "Parenthood" ↔ "Parent-Child Relationship" and
    "Marriage" ↔ "Marriage Start", producing parallel State/Event hubs
    across windows.

    Decision tiers:
      1. Same label (case-insensitive trim) → KEEP — same standardized
         bucket, hand to LLM to verify (the bucket conflates instances).
      2. Different label BUT cosine similarity of sentence embeddings
         >= LABEL_SIMILARITY_THRESHOLD → KEEP — semantically related,
         worth LLM verification.
      3. Different label AND embedding similarity below threshold (or
         embedding unavailable) → DROP — strong evidence of unrelated
         instances, save the LLM call.

    Failure-isolated: chroma/embedder down → degrade to label-only
    (matches old behavior), log warning.

    Args:
        scored: candidate list as returned by
            ``_score_candidates_by_participant_overlap``.
        new_label: the new proposal's label.
        new_sentence: the new proposal's sentence (canonical or raw —
            either is fine, embeddings are robust to small phrasing diffs).

    Returns:
        Filtered scored list, same shape.
    """
    new_norm = (new_label or "").strip().casefold()
    if not new_norm:
        return scored  # no new label to compare against — pass through

    # Tier 1: same label
    same_label = []
    diff_label = []
    for s in scored:
        cand_norm = (getattr(s["node"], "label", "") or "").strip().casefold()
        if cand_norm == new_norm:
            same_label.append(s)
        else:
            diff_label.append(s)

    if not diff_label or not new_sentence or not new_sentence.strip():
        return same_label

    # Tier 2: embedding similarity for different-label candidates
    try:
        from app.assistant.embeddings.embedder import embed_text
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        import numpy as np
    except Exception as exc:
        logger.warning(
            "[promoter] embedding similarity unavailable for label-relax filter: %s "
            "(falling back to strict label-only)", exc,
        )
        return same_label

    try:
        new_emb = np.array(embed_text(new_sentence), dtype=float)
        new_n = float(np.linalg.norm(new_emb))
        if new_n == 0.0:
            return same_label
        cm = get_chroma_manager()
        cand_ids = [s["node"].id for s in diff_label]
        result = cm.node_context_collection.get(
            ids=cand_ids, include=["embeddings"]
        )
        emb_by_id: Dict[str, list] = {}
        for cid, emb in zip(result.get("ids") or [], result.get("embeddings") or []):
            if emb is not None and len(emb) > 0:
                emb_by_id[cid] = emb
        kept_diff = []
        for s in diff_label:
            cand_emb = emb_by_id.get(s["node"].id)
            if cand_emb is None:
                continue  # no embedding → can't compare → drop (conservative)
            cand_v = np.array(cand_emb, dtype=float)
            cand_n = float(np.linalg.norm(cand_v))
            if cand_n == 0.0:
                continue
            sim = float(np.dot(new_emb, cand_v) / (new_n * cand_n))
            if sim >= _LABEL_SIMILARITY_THRESHOLD:
                kept_diff.append(s)
        if kept_diff:
            logger.info(
                "[promoter] label-relax kept %d diff-label candidate(s) via "
                "embedding similarity (threshold=%.2f)",
                len(kept_diff), _LABEL_SIMILARITY_THRESHOLD,
            )
        return same_label + kept_diff
    except Exception as exc:
        logger.warning(
            "[promoter] label-relax embedding lookup failed: %s "
            "(falling back to strict label-only)", exc,
        )
        return same_label


# Back-compat alias for the old name; deprecate in a follow-up.
def _filter_candidates_by_label_equality(scored, new_label):
    return _filter_candidates_by_label_or_similarity(scored, new_label, None)


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


def _resolved_window_text(session, window_id: str, max_chars: int = 600) -> Optional[str]:
    """Reconstruct the entity-resolved prose for a window.

    Walks kg_window_message → kg_resolved_message and returns the
    `speaker: text` joined snippet using the resolver's entity-enriched
    output. Falls back per-message to raw `unified_log_2026.message` when
    a row hasn't been resolved yet (transitional). Returns None if the
    window has no messages or window_id is empty.

    This is the canonical "what did the chat actually say, with entities
    expanded" view. Every agent in the KG pipeline that needs window
    context should consume THIS, not raw source_text — per the
    `feedback_full_resolved_window_beats_fragments` rule.
    """
    if not window_id:
        return None
    try:
        from sqlalchemy import text as sql_text
        rows = session.execute(
            sql_text(
                "SELECT ul.role, ul.speaker_name, "
                "       COALESCE(rm.resolved_text, ul.message) AS text "
                "FROM kg_window_message wm "
                "JOIN unified_log_2026 ul ON ul.id = wm.unified_log_id "
                "LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = wm.unified_log_id "
                "WHERE wm.window_id = :w "
                "ORDER BY wm.item_order"
            ),
            {"w": window_id},
        ).fetchall()
        if not rows:
            return None
        lines: list[str] = []
        for role, speaker, txt in rows:
            t = (txt or "").strip()
            if not t:
                continue
            who = (speaker or role or "user").strip() or "user"
            lines.append(f"{who}: {t}")
        if not lines:
            return None
        out = "\n".join(lines)
        return out if len(out) <= max_chars else out[:max_chars] + "…"
    except Exception:
        return None


def _node_window_text(session, node_id: str, max_chars: int = 600) -> Optional[str]:
    """Entity-resolved chat context for the node's earliest evidence window.

    Gives the node_data_merger the actual disambiguated chat the node was
    extracted from. With entity references expanded ("(Peter)" inline
    instead of "he"), the merger no longer has to redo coreference and
    can judge same-label vs different-target with the same information
    the resolver already produced.

    Falls back to the raw `kg_node_evidence.source_text` snapshot when no
    window_id is present (legacy rows from before the resolver was wired).
    Returns None if no evidence row exists.
    """
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT source_text, window_id FROM kg_node_evidence "
                "WHERE node_id = :nid "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"nid": node_id},
        ).fetchone()
        if not row:
            return None
        source_text, window_id = row
        resolved = _resolved_window_text(session, window_id, max_chars=max_chars) if window_id else None
        if resolved:
            return resolved
        if not source_text:
            return None
        txt = str(source_text)
        return txt if len(txt) <= max_chars else txt[:max_chars] + "…"
    except Exception:
        return None


def _proposal_window_text(session, proposal_id: str, max_chars: int = 600) -> Optional[str]:
    """Entity-resolved chat context for the proposal's earliest evidence
    window — the new-side counterpart of _node_window_text.

    Falls back to the raw `claim_proposal_evidence.raw_text` snapshot when
    no window_id is recorded for the proposal."""
    try:
        from sqlalchemy import text as sql_text
        row = session.execute(
            sql_text(
                "SELECT raw_text, window_id FROM claim_proposal_evidence "
                "WHERE proposal_id = :pid "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"pid": proposal_id},
        ).fetchone()
        if not row:
            return None
        raw_text, window_id = row
        resolved = _resolved_window_text(session, window_id, max_chars=max_chars) if window_id else None
        if resolved:
            return resolved
        if not raw_text:
            return None
        txt = str(raw_text)
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
        from app.assistant.scope.loader import load_scope_for_source
        from app.assistant.utils.pydantic_classes import Message
    except Exception as exc:
        logger.warning("[promoter] node_merger unavailable: %s", exc)
        return None

    agent = DI.agent_factory.create_agent("knowledge_graph_add::node_merger")
    if agent is None:
        logger.warning("[promoter] could not create node_merger agent")
        return None

    # "Reader #3" (audit P2.5): when the maintenance loop already ruled two
    # of these candidates DISTINCT from each other, tell the merger — it
    # would otherwise happily re-bind a new observation into the wrong
    # twin, undoing maintenance work indefinitely. Memo-inject rather than
    # drop: the verdict says the CANDIDATES differ from each other, not
    # which (if either) matches the new observation — that judgment stays
    # with the LLM (deterministic proposes, LLM decides).
    try:
        from app.assistant.kg_maintenance.verdict_store import (
            load_distinct_verdicts_among,
        )
        cand_ids = [str(c.get("node_id") or "") for c in candidate_payload]
        verdicts = load_distinct_verdicts_among(cand_ids)
        if verdicts:
            notes_by_id: Dict[str, List[str]] = {}
            for a, b, memo, decided_by in verdicts:
                line = (
                    f"prior verdict ({decided_by}): DISTINCT from candidate "
                    f"{{other}} — {memo}"
                )
                notes_by_id.setdefault(a, []).append(line.format(other=b[:8]))
                notes_by_id.setdefault(b, []).append(line.format(other=a[:8]))
            for c in candidate_payload:
                notes = notes_by_id.get(str(c.get("node_id") or ""))
                if notes:
                    c["prior_distinct_verdicts"] = notes
            logger.info(
                "[promoter] injected %d distinct-verdict note(s) into "
                "node_merger candidates", len(verdicts),
            )
    except Exception as exc:
        logger.warning("[promoter] verdict-store consult failed: %s", exc)

    scope = load_scope_for_source(
        kind="pipeline", source_id="kg_pipeline", actor_id="proposal_promoter",
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

    # Goal lifecycle: last_pursued_at is now a first-class column on Node
    # (promoted from attributes JSON 2026-05-11). Stamped on Goal nodes from
    # proposal_node.valid_from. The dormancy sweep + recency UI read the
    # column directly. `goal_status` (also first-class) is the canonical
    # status field (vocabulary: active | dormant | completed | abandoned).
    proposed_last_pursued_at = None
    if proposal_node.node_type == "Goal" and proposal_node.valid_from:
        proposed_last_pursued_at = proposal_node.valid_from

    sentence_for_node = (proposal_node.sentence or "") if hasattr(proposal_node, "sentence") else ""
    if proposal_node.node_type in {"State", "Event", "Goal"} and canonical_sentence:
        sentence_for_node = canonical_sentence

    # goal_status is a first-class column on Node — copy it through
    # from the proposal's attributes (meta_data_add stamps it for Goal
    # nodes). Default to 'active' on creation when not provided.
    proposed_goal_status = (
        attrs.pop("goal_status", None)
        if isinstance(attrs, dict) else None
    )
    if proposal_node.node_type == "Goal" and not proposed_goal_status:
        proposed_goal_status = "active"

    # semantic_label is also a first-class column. meta_data_add produces it,
    # enrich_extraction carries it into attributes_json, but historically the
    # promoter never popped it out — leaving 36% of nodes with NULL
    # semantic_label and the value buried in the JSON blob. Pop it now.
    proposed_semantic_label = (
        attrs.pop("semantic_label", None)
        if isinstance(attrs, dict) else None
    )

    # importance: not set at promotion time. Nodes land with NULL importance.
    # The dedicated raters (me::importance_rater for Entity, regenerate_state_importance
    # for State/Event/Goal/Property derivation) fill it in via the
    # _lazy_kg_importance_rater routine that runs periodically — that path
    # preserves cross-node calibration which a per-promotion rating would lose.
    # (Historical: meta_data_add used to emit importance as a side-effect, and
    # this function ×10'd and copied it through. Both were removed 2026-05-11
    # in favor of the dedicated-rater-only path.)

    # confidence + valid_during are first-class columns on Node but were
    # historically left in attributes_json — audit 2026-05-11 found 0/3026
    # rows had Node.confidence / Node.valid_during set, while the JSON had
    # them for 997/1000 and 737/1000 respectively. Same fix pattern: pop and
    # promote.
    proposed_confidence: Optional[float] = None
    if isinstance(attrs, dict):
        raw_conf = attrs.pop("confidence", None)
        try:
            if raw_conf is not None:
                proposed_confidence = float(raw_conf)
        except (TypeError, ValueError):
            proposed_confidence = None
    proposed_valid_during: Optional[str] = (
        attrs.pop("valid_during", None) if isinstance(attrs, dict) else None
    )
    if proposed_valid_during is not None:
        proposed_valid_during = str(proposed_valid_during).strip() or None

    # Observation lifecycle (promoted to first-class columns 2026-05-11).
    # first_observed comes from the originating chat message timestamp, not
    # node-creation time. observation_count starts at 1.
    proposed_first_observed = None
    proposed_last_observed = None
    if isinstance(attrs, dict):
        # Strip any stale JSON values (older proposals may carry them).
        attrs.pop("first_observed", None)
        attrs.pop("last_observed", None)
        attrs.pop("observation_count", None)
    # Pull from the proposal row directly — it's the canonical source.
    obs_anchor = (
        getattr(proposal_node, "valid_from", None)
        or getattr(proposal_node, "created_at", None)
    )
    if obs_anchor is not None:
        proposed_first_observed = obs_anchor

    new = Node(
        label=proposal_node.label,
        node_type=proposal_node.node_type,
        goal_status=proposed_goal_status,
        semantic_label=proposed_semantic_label,
        # importance intentionally not set — filled in post-promotion by the
        # periodic me::importance_rater pass (cross-node calibration > per-node
        # isolation).
        confidence=proposed_confidence,
        valid_during=proposed_valid_during,
        first_observed=proposed_first_observed,
        last_pursued_at=proposed_last_pursued_at,
        # last_observed stays NULL until first re-observation.
        # observation_count defaults to 1 (column default).
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
        start_date_confidence=_effective_start_date_confidence(
            session, proposal_id, proposal_node
        ),
        end_date_confidence=proposal_node.end_date_confidence,
        start_date_prose=proposal_node.valid_from_prose,
        end_date_prose=proposal_node.valid_to_prose,
        # Validity assessment from meta_data_add (State/Event) or
        # deterministic goal_status path (Goal). Null when no closing
        # evidence; False when explicitly bounded. Never True.
        valid_currently=proposal_node.valid_currently,
        validity_reason=proposal_node.validity_reason,
        source="proposal_promoter",
        created_from_proposal_id=proposal_id,
    )
    session.add(new)
    session.flush()
    # Embed-at-write (2026-05-12): immediately compute and store the
    # node's context embedding in ChromaDB so it's discoverable by Tier 3
    # of the maintenance duplicate scan on the very next run, instead of
    # waiting up to 7 days for the periodic context_embedding_backfill.
    # Failure-isolated — chroma down / model unavailable / embedding error
    # all log a warning and let the node persist. The backfill step
    # remains the safety net.
    _embed_and_store_node(new.id, new.label, sentence_for_node)
    return new


def _embed_and_store_node(node_id: str, label: str, sentence: str) -> None:
    """Compute the label + context embeddings for a freshly-written node
    and store them in ChromaDB. No-op + warn on any failure.

    The label embedding was historically only written by
    KnowledgeGraphUtils.add_node — a path the promoter doesn't use — which
    is how the node_embeddings collection ended up EMPTY after the
    embedding-schema reset (found 2026-06-10): the promoter creates nearly
    every node now and only wrote context vectors. Both collections feed
    the duplicate scan's tier 3 and the entity semantic tier (P2.3).

    Single responsibility: don't query, don't decide; just persist the
    embeddings. The dedup pipelines that consume them remain the policy
    layer.
    """
    try:
        from app.assistant.embeddings.embedder import embed_text
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    except Exception as exc:
        logger.warning("[promoter] embedding modules unavailable: %s", exc)
        return
    try:
        cm = get_chroma_manager()
        if label and str(label).strip():
            cm.store_node_embedding(node_id, label, embed_text(label))
        if sentence and str(sentence).strip():
            cm.store_node_context_embedding(node_id, sentence, embed_text(sentence))
    except Exception as exc:
        logger.warning(
            "[promoter] failed to embed/store node %s: %s "
            "(the periodic backfill step is the safety net)",
            node_id[:8] if node_id else "?", exc,
        )


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
        from app.assistant.scope.loader import load_scope_for_source
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

    scope = load_scope_for_source(
        kind="pipeline", source_id="kg_pipeline", actor_id="proposal_promoter",
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
        "estimated_at": utc_now().isoformat(),
    }


def _refresh_on_reobservation(
    node: Node, proposal: ClaimProposal,
    proposal_node: Optional[ClaimProposalNode] = None,
) -> None:
    """Bump the matched node's observation-tracking columns + attributes.

    Called on ``matched_existing`` in the promoter. Without this the decay
    step can't tell "re-observed last week" from "nobody's mentioned this
    in 6 months" — the ``updated_at`` column doesn't distinguish.

    Updates first-class columns (promoted from JSON 2026-05-11):
      - ``Node.last_observed`` — monotonic max(existing, observed); decay
        reads ONLY this column, and a blind overwrite while promoting a
        backlog of OLDER windows used to rewind it on a live state, which
        decay then auto-closed (audit P2.2).
      - ``Node.first_observed`` — only set if missing (back-compat for
        legacy nodes pre-attribute-tracking).
      - ``Node.observation_count`` — increment by 1.
      - ``Node.confidence`` — gentle bump (+0.05, capped at 1.0) but
        only if already set; don't invent a confidence from nothing.

    A proposal with no observation timestamps (the writer's window anchor
    can legitimately come back empty) still IS a re-observation — the
    stamp anchors on the earliest evidence row's observed_at, else now.
    Silently skipping the stamp let decay close states the user had just
    re-confirmed (audit P2.2).

    Refinement (audit P2.2(c), needs ``proposal_node``): the matched node's
    NULL validity fields fill from the proposal (never overwriting non-NULL
    values — decay otherwise anchors on created_at forever), and the
    proposal's label folds into aliases when it differs from the node's
    label (feeds tier-1 of the duplicate scan). Refinement IS a content
    change, so when it fires updated_at bumps normally; pure re-observation
    bookkeeping still preserves updated_at verbatim.

    Goal lifecycle (also first-class as of 2026-05-11):
      - ``Node.last_pursued_at`` — monotonic, bumped on Goal re-observation.

    CRITICAL: pure bookkeeping must NOT bump Node.updated_at. Bumping it
    marks every wiki + entity card whose neighborhood includes this node
    as "changed" (see kg_projection.change_detection), cascading refreshes
    on no semantic change. Mutating ``node.attributes`` via ORM would
    trigger SQLAlchemy's onupdate=func.now() hook on commit, so we issue
    an explicit UPDATE with ``updated_at = Node.updated_at`` self-ref to
    preserve the timestamp verbatim. Same pattern as persist_description.
    """
    from sqlalchemy.orm import object_session as _object_session
    _session_for_anchor = _object_session(node)

    observed = proposal.last_observed_at or proposal.first_observed_at
    if observed is None:
        ev = (
            _earliest_proposal_evidence(_session_for_anchor, proposal.id)
            if _session_for_anchor is not None else None
        )
        observed = (
            getattr(ev, "observed_at", None)
            or getattr(ev, "created_at", None)
            or utc_now()
        )

    # Compute the column updates from the existing values.
    # Monotonic: promoting an older backlog window must never rewind the
    # recency signal on a node that was re-observed more recently.
    existing_last = node.last_observed
    new_last_observed = (
        observed if (existing_last is None or observed > existing_last)
        else existing_last
    )
    new_first_observed = node.first_observed or observed
    new_observation_count = int(node.observation_count or 1) + 1
    new_confidence = node.confidence
    if new_confidence is not None:
        try:
            new_confidence = min(1.0, float(new_confidence) + 0.05)
        except (TypeError, ValueError):
            pass

    # Goal lifecycle: any re-observation revives a dormant Goal back to
    # active and bumps the recency signal the dormancy sweep reads.
    # Terminal closures (column goal_status in {"completed", "abandoned"})
    # must NOT be silently reopened — surface as a finding instead.
    # `goal_status` AND `last_pursued_at` are both first-class columns
    # now (last_pursued_at promoted from JSON 2026-05-11).
    revive_to_active = False
    new_last_pursued_at = node.last_pursued_at
    if (node.node_type or "") == "Goal":
        # Monotonic for the same backlog-rewind reason as last_observed.
        if new_last_pursued_at is None or observed > new_last_pursued_at:
            new_last_pursued_at = observed
        cur_status = (node.goal_status or "").lower()
        if cur_status not in ("completed", "abandoned"):
            revive_to_active = True

    # Refinement (audit P2.2(c)): fill the matched node's NULL validity
    # fields from the proposal and fold a differing proposal label into
    # aliases. Never overwrites a non-NULL value.
    refinement: Dict[str, Any] = {}
    if proposal_node is not None:
        for field, value in (
            ("start_date", proposal_node.valid_from),
            ("end_date", proposal_node.valid_to),
            ("start_date_prose", proposal_node.valid_from_prose),
            ("end_date_prose", proposal_node.valid_to_prose),
            ("start_date_confidence", proposal_node.start_date_confidence),
            ("end_date_confidence", proposal_node.end_date_confidence),
        ):
            if value is not None and getattr(node, field, None) is None:
                refinement[field] = value
        p_label = (proposal_node.label or "").strip()
        if p_label and p_label.lower() != (node.label or "").strip().lower():
            aliases = list(node.aliases or [])
            if p_label.lower() not in {str(a).lower() for a in aliases if a}:
                refinement["aliases"] = aliases + [p_label]
                # A new alias means new pairing potential for the dupe scan.
                refinement["last_dupe_scanned_at"] = None

    from sqlalchemy import update as sql_update
    from sqlalchemy.orm import object_session
    session = object_session(node)
    if session is None:
        # Defensive branch: no session in scope means we can't issue our
        # explicit UPDATE. Leave the ORM-tracked mutation as the write path;
        # caller's commit will flush it (with the unwanted updated_at bump).
        # This branch shouldn't fire in production — _refresh_on_reobservation
        # is only called from _evaluate_and_apply with a live session.
        node.last_observed = new_last_observed
        node.first_observed = new_first_observed
        node.observation_count = new_observation_count
        if new_confidence is not None:
            node.confidence = new_confidence
        if new_last_pursued_at is not None:
            node.last_pursued_at = new_last_pursued_at
        if revive_to_active:
            node.goal_status = "active"
        for field, value in refinement.items():
            setattr(node, field, value)
        return
    update_values = {
        "last_observed": new_last_observed,
        "first_observed": new_first_observed,
        "observation_count": new_observation_count,
    }
    if refinement:
        # Refinement is a real content change — let the column-level
        # onupdate bump updated_at so wiki/card change detection sees it.
        update_values.update(refinement)
    else:
        # Pure bookkeeping — preserve updated_at verbatim via self-ref.
        update_values["updated_at"] = Node.updated_at
    if new_confidence is not None:
        update_values["confidence"] = new_confidence
    if new_last_pursued_at is not None:
        update_values["last_pursued_at"] = new_last_pursued_at
    if revive_to_active:
        update_values["goal_status"] = "active"
    session.execute(
        sql_update(Node)
        .where(Node.id == node.id)
        .values(**update_values)
    )
    # Force ORM to reload columns on next access so downstream callers
    # see post-update values rather than cached pre-update ones.
    session.expire(node, [
        "last_observed", "first_observed", "observation_count",
        "confidence", "last_pursued_at",
        "start_date", "end_date", "start_date_prose", "end_date_prose",
        "start_date_confidence", "end_date_confidence", "aliases",
    ])


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
        from app.assistant.scope.loader import load_scope_for_source
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

    scope = load_scope_for_source(
        kind="pipeline", source_id="kg_pipeline", actor_id="proposal_promoter",
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


def _effective_start_date_confidence(
    session, proposal_id: str, proposal_node: ClaimProposalNode,
) -> Optional[str]:
    """Decide what start_date_confidence should land on the new kg_node row.

    Guardrail for the chat-stamp default: meta_data_add and date-fill agents
    are supposed to emit 'inferred' (or 'estimated') whenever they derive
    a start_date from prose with no explicit calendar phrase. They drift
    toward 'actual' anyway, so a backfill audit ran in 2026-05-11
    (scripts/audit_actual_dates_no_explicit_evidence.py) and downgraded
    285 rows. This helper enforces the same rule at promote time so the
    bad label never lands.

    Rule: for State/Event/Goal nodes carrying start_date_confidence='actual',
    if the start_date's calendar day matches any of this proposal's
    evidence row chat dates (observed_at), downgrade to 'inferred'.
    Past-event recall ("we got married 2003-09-09" mentioned in a 2026
    chat) keeps 'actual' because start_date != chat_date.

    Returns the value to use for start_date_confidence (string or None).
    """
    raw = proposal_node.start_date_confidence
    if (raw or "").lower() != "actual":
        return raw
    if proposal_node.node_type not in {"State", "Event", "Goal"}:
        return raw
    sd = proposal_node.valid_from
    if sd is None:
        return raw
    sd_iso = sd.strftime("%Y-%m-%d") if hasattr(sd, "strftime") else str(sd)[:10]
    if len(sd_iso) < 10:
        return raw

    from app.assistant.database.claim_proposals import ClaimProposalEvidence
    rows = (
        session.query(ClaimProposalEvidence.observed_at)
        .filter(ClaimProposalEvidence.proposal_id == proposal_id)
        .all()
    )
    chat_dates: set[str] = set()
    for (ts,) in rows:
        if ts is None:
            continue
        if hasattr(ts, "strftime"):
            chat_dates.add(ts.strftime("%Y-%m-%d"))
        else:
            s = str(ts)
            if len(s) >= 10:
                chat_dates.add(s[:10])

    if sd_iso in chat_dates:
        logger.info(
            "[promoter] downgrade start_date_confidence actual→inferred "
            "(chat-stamp default) for %s %r start_date=%s",
            proposal_node.node_type, proposal_node.label, sd_iso,
        )
        return "inferred"
    return raw


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

    Provenance is window-level only (window_id). The legacy source_table
    / source_id pair was copied from claim_proposal_evidence.unified_log_id
    which was itself the first user message of the window — not the actual
    source of any particular claim. Multi-topic windows produced
    misattributions that downstream consumers silently treated as truth.
    Source context now comes from walking window_id -> kg_window_message
    -> unified_log_2026.
    """
    try:
        from app.assistant.database.kg_chat_projection import KGNodeEvidence
    except Exception as exc:
        logger.warning("[promoter] node evidence cascade unavailable: %s", exc)
        return

    ev = _earliest_proposal_evidence(session, proposal.id)
    session.add(KGNodeEvidence(
        node_id=node_id,
        source_table=None,
        source_id=None,
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
        source_table=None,
        source_id=None,
        source_text=(ev.raw_text if ev else None),
        derived_sentence=proposal_edge.sentence,
        message_timestamp=(ev.observed_at if ev else None),
        window_id=(ev.window_id if ev else None),
        merge_action=merge_action,
    ))


def _existing_kg_edge(
    session, src_id: str, tgt_id: str, predicate: str,
    *, bidirectional: bool = False,
) -> Optional[Edge]:
    """Find an existing edge asserting the same fact (audit P2.4).

    Matches the predicate's whole spelling class (canonical + every known
    alias — derived from the alias maps, so legacy rows written before an
    alias existed still dedup), and for direction-free predicates
    (edge_canon.is_symmetric, or the writer's per-edge ``bidirectional``
    flag) also the reversed triple — a reversed re-assertion must
    reinforce, not mint a mirror twin.
    """
    from app.assistant.kg.predicate_vocabulary import (
        is_symmetric_predicate,
        predicate_spelling_class,
    )
    members = predicate_spelling_class(predicate)
    hit = (
        session.query(Edge)
        .filter(
            Edge.source_id == src_id,
            Edge.target_id == tgt_id,
            Edge.relationship_type.in_(members),
        )
        .first()
    )
    if hit is not None:
        return hit
    if bidirectional or is_symmetric_predicate(predicate):
        return (
            session.query(Edge)
            .filter(
                Edge.source_id == tgt_id,
                Edge.target_id == src_id,
                Edge.relationship_type.in_(members),
            )
            .first()
        )
    return None


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
        # (is_married / married_to are normalized to is_spouse_in by the
        # writer AND by the normalize call below — dead entries removed,
        # audit P3.6.)
        "is_spouse_in",
        # Primary employer — debatable for contractors but usually
        # single-target in a biographical graph. employed_by collapses to
        # works_for; the synonym CLASS is checked below (audit P2.4).
        "works_for",
        # Hard biological facts — single-target.
        "born_in",           # one birthplace
        "has_birthday",      # one date of birth
        "has_nationality",   # dual citizenship is rare; flag if multiple show up
        # Broader semantic contradictions (conflicting beliefs, conflicting
        # locations, habits) need LLM judgment — deliberately NOT covered
        # here. A perplexity-check agent is the right home for those.
    }
    # Normalize so raw spellings (employed_by, is_married) land on their
    # canonical class even when a caller bypassed the writer.
    from app.assistant.kg.predicate_vocabulary import (
        normalize_predicate,
        predicate_spelling_class,
    )
    canonical, _ = normalize_predicate(predicate)
    if canonical not in SINGLE_TARGET_PREDICATES:
        return None
    hit = (
        session.query(Edge)
        .filter(
            Edge.source_id == src_id,
            Edge.relationship_type.in_(predicate_spelling_class(canonical)),
        )
        .first()
    )
    if hit is None:
        return None
    if hit.target_id == incoming_tgt_id:
        return None
    return hit


def _incoming_era_start(pnodes_by_id, pe) -> Optional[datetime]:
    """The proposal-side era start for an incoming single-target edge: the
    ``valid_from`` of the relationship-like pnode at either endpoint (the
    spouse shape puts the State on the target, the employment shape on the
    source). None when the proposal carries no dates."""
    for endpoint_id in (pe.source_node_id, pe.target_node_id):
        pn = pnodes_by_id.get(endpoint_id)
        if pn is not None and pn.node_type in RELATIONSHIP_LIKE_TYPES \
                and pn.valid_from is not None:
            return pn.valid_from
    return None


def _classify_durable_conflict(
    session, conflict: Edge, src_id: str,
    incoming_era_start: Optional[datetime],
) -> tuple[str, Optional[Node]]:
    """Temporal triage of a single-target conflict (audit P2.1).

    The era of the EXISTING assertion lives on the State/Event endpoint of
    the conflicting edge — the old-target side in the spouse shape
    (person → Marriage State), the shared source side in the employment
    shape (Employment State → employer).

    Returns (kind, era_node):
      - "closed_era"  — the existing era has end_date set; the new
        assertion is history-compatible, NOT a conflict. (Remarriage after
        a closed marriage; new job after a closed employment.)
      - "succession"  — the existing era is open but the proposal's dates
        postdate its start; likely a real-life change the KG hasn't closed
        yet. Conservative path: hold + route to the user (P4.4 decision).
      - "same_era"    — undatable or overlapping double assertion; a
        genuine contradiction at edge granularity.
    """
    era_node: Optional[Node] = None
    old_tgt = session.get(Node, conflict.target_id)
    if old_tgt is not None and (old_tgt.node_type or "") in ("State", "Event"):
        era_node = old_tgt
    else:
        src_node = session.get(Node, src_id)
        if src_node is not None and (src_node.node_type or "") in ("State", "Event"):
            era_node = src_node

    if era_node is not None and era_node.end_date is not None:
        return "closed_era", era_node

    old_start = era_node.start_date if era_node is not None else None
    if (
        incoming_era_start is not None
        and old_start is not None
        and incoming_era_start > old_start
    ):
        return "succession", era_node

    return "same_era", era_node


# ---------------------------------------------------------------------------
# Decision container
# ---------------------------------------------------------------------------

class _NodeOutcome:
    __slots__ = ("pnode_id", "action", "resolved_node_id", "reason")
    def __init__(self, pnode_id, action, resolved_node_id=None, reason=""):
        self.pnode_id = pnode_id
        self.action = action  # matched_existing | created_new | skipped_locked | skipped_conflict
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
    __slots__ = ("proposal_id", "final_status", "node_outcomes", "edge_outcomes",
                 "error", "followup_findings")
    def __init__(self, proposal_id):
        self.proposal_id = proposal_id
        self.final_status = "pending"
        self.node_outcomes: List[_NodeOutcome] = []
        self.edge_outcomes: List[_EdgeOutcome] = []
        self.error: Optional[str] = None
        # kg_maintenance_finding payloads (kwargs for upsert_finding) the
        # caller writes AFTER its write transaction commits — single_target
        # succession/conflict routing (audit P2.1). Must reference only
        # PRE-EXISTING node ids: on 'held' the savepoint rolls freshly
        # minted nodes back.
        self.followup_findings: List[Dict[str, Any]] = []

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
    # True when the match came from a node_merger CONFIRMATION (alias-tier /
    # semantic-tier / person-confirm). Confirmed binds mint mention-map
    # entries in the apply phase (identity phase 5) so the next mention of
    # the same form resolves closed-form.
    from_confirm: bool = False
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
        # The definite description — the strongest same-referent signal the
        # merger gets (graph-derived; see identity_sentence.py). NULL until
        # the nightly refresh has covered this node.
        "identity_sentence": getattr(cand, "identity_sentence", None),
        "description": (cand.description or "")[:500],
        "original_sentence": cand.original_sentence,
        "start_date": cand.start_date.isoformat() if cand.start_date else None,
        "end_date": cand.end_date.isoformat() if cand.end_date else None,
        "start_date_prose": cand.start_date_prose,
        "end_date_prose": cand.end_date_prose,
        "first_observed": cand.first_observed.isoformat() if cand.first_observed else None,
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
    # Entity-like pnodes whose bind needs post-session node_merger
    # confirmation (audit P2.3): person-category same-label hits with
    # substantial edges + semantic near-matches with no exact hit.
    entity_confirm_lists: Dict[str, List[Dict[str, Any]]] = {}
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
                resolved = _resolve_entity_like(session, pn.label, pn.node_type)
                m, tier = resolved if resolved is not None else (None, None)
                if m is not None and tier == "alias":
                    # Alias hits are NOT closed-form identity: aliases
                    # accrete from bound mention labels (P2.2(c)), so a
                    # generic label ("House") can become an alias of one
                    # specific node and capture every future mention of
                    # anyone's house (the wrong-house misbind, 2026-06-12).
                    # The sentence decides: route through node_merger with
                    # the alias hit + semantic near-matches as candidates.
                    entity_resolutions[pn.id] = None
                    confirm = [_snapshot_state_candidate(session, m)]
                    seen_ids = {m.id}
                    for c in _semantic_entity_candidates(
                        session, pn.label, pn.node_type,
                        sentence=getattr(pn, "sentence", None),
                    ):
                        if c.get("node_id") not in seen_ids:
                            confirm.append(c)
                            seen_ids.add(c.get("node_id"))
                    entity_confirm_lists[pn.id] = confirm
                elif m is not None and tier != "mention_map" \
                        and _needs_person_confirm(session, m):
                    # Two-Alexes guard (audit P2.3): a same-label bind to a
                    # well-connected person needs node_merger confirmation
                    # (post-session). Unconfirmed → create; a duplicate is
                    # fixable, a wrong person-merge is not. Mention-map hits
                    # skip this — the map IS a recorded confirmation and
                    # self-revokes if the form ever becomes contested.
                    # Known tradeoff: while unconfirmed, this entity is
                    # absent from entity_resolutions, so THIS proposal's
                    # State/Event participant fingerprints (computed below,
                    # in-session) don't include it. Edges still resolve
                    # correctly at apply time via the post-session decision.
                    entity_resolutions[pn.id] = None
                    entity_confirm_lists[pn.id] = [
                        _snapshot_state_candidate(session, m)
                    ]
                elif m is not None:
                    entity_resolutions[pn.id] = m.id
                else:
                    entity_resolutions[pn.id] = None
                    # Semantic tier (audit P2.3): no exact label/alias hit —
                    # near-matches ("the user's mom" vs "...mother") go to
                    # the post-session node_merger instead of silently
                    # minting a twin.
                    sem = _semantic_entity_candidates(
                        session, pn.label, pn.node_type,
                        sentence=getattr(pn, "sentence", None),
                    )
                    if sem:
                        entity_confirm_lists[pn.id] = sem

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

            # Label-mismatch exclusion (relaxed 2026-05-12).
            # Strict label-equality dropped legitimate same-concept matches
            # like "Parenthood" ↔ "Parent-Child Relationship" or
            # "Marriage" ↔ "Marriage Start" → parallel State hubs accumulated.
            # New behavior: keep candidates with same label OR with sentence-
            # embedding cosine similarity >= LABEL_SIMILARITY_THRESHOLD (0.7).
            # LLM verification (`node_merger`) downstream is the final arbiter.
            scored = _filter_candidates_by_label_or_similarity(
                scored, pn.label, pn.sentence,
            )

            # Cap LLM input — hub participants (Jukka) explode candidate count.
            candidate_lists[pn.id] = [
                _snapshot_state_candidate(session, s["node"]) for s in scored[:5]
            ]

    # ---- Read session closed. Phase 2: LLM calls, no session held. ----
    nodes: Dict[str, _PreparedNode] = {}

    # Entity-like nodes: exact resolutions are match-or-create with no LLM;
    # confirm-tier pnodes (person same-label guard / semantic near-match,
    # audit P2.3) go through node_merger — same machinery as State/Event.
    for pn_id, snap in pnode_snaps.items():
        if snap["node_type"] not in ENTITY_LIKE_TYPES:
            continue
        match_id = entity_resolutions.get(pn_id)
        from_confirm = False
        if match_id is None and entity_confirm_lists.get(pn_id):
            new_node_ctx = {
                "label": snap["label"],
                "node_type": snap["node_type"],
                "category": snap["category"],
                "description": snap["description_draft"],
                "original_sentence": snap["sentence"],
                "source_window_id": proposal_window_id,
                "source_window_text": proposal_window_text,
            }
            match_id = _call_node_merger_for_state_match(
                new_node_ctx, entity_confirm_lists[pn_id],
            )
            from_confirm = match_id is not None
        nodes[pn_id] = _PreparedNode(
            pn_id=pn_id,
            pn_label=snap["label"] or "",
            pn_node_type=snap["node_type"],
            decision="match" if match_id else "create",
            matched_node_id=match_id,
            from_confirm=from_confirm,
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
                # first_observed = valid_from on the proposal (canonical
                # source; previously buried in attributes JSON).
                "first_observed": snap["valid_from"].isoformat() if snap["valid_from"] else None,
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
                    _refresh_on_reobservation(match_node, proposal, proposal_node=pn)
                _write_node_evidence(
                    session,
                    node_id=prepared.matched_node_id,
                    proposal=proposal,
                    proposal_node=pn,
                    merge_action="confirmed",
                )
                if prepared.from_confirm:
                    # Identity phase 5: the node_merger confirmed this
                    # mention form → record it durably so the next mention
                    # binds closed-form. mint refuses contested forms
                    # (sink, not error).
                    try:
                        from app.assistant.kg.mention_map import mint_mention
                        mint_mention(
                            session,
                            label=pn.label,
                            node_type=pn.node_type,
                            node_id=prepared.matched_node_id,
                            minted_by="node_merger:confirm",
                            source_proposal_id=proposal.id,
                        )
                    except Exception as exc:
                        logger.error(
                            "[promoter] mention-map mint failed for %r: %s",
                            pn.label, exc,
                        )
            dec.node_outcomes.append(_NodeOutcome(
                pn.id, "matched_existing", prepared.matched_node_id,
                f"{pn.node_type} {pn.label!r} matched {prepared.matched_node_id[:8]}",
            ))
        else:
            if commit:
                # Mint/match race guard (audit P2.6): the plan decided
                # "create" against a read snapshot taken before the LLM
                # calls; a concurrent run or manual approve may have minted
                # this entity meanwhile. Cheap re-check inside the write
                # txn; on hit, convert to match instead of double-minting.
                # Property stays out: it is subject-scoped (never bound by
                # global label) and resolves via _resolve_property only.
                raced_hit = (
                    _resolve_entity_like(session, pn.label, pn.node_type)
                    if pn.node_type != "Property" else None
                )
                raced, raced_tier = raced_hit if raced_hit is not None else (None, None)
                # Alias-tier raced hits don't convert: a silent alias bind is
                # the exact failure the confirm tier exists to prevent. A
                # twin node is the safe outcome (dup scan heals it later).
                if raced is not None and raced_tier != "alias" and not _needs_person_confirm(session, raced):
                    pn.resolved_node_id = raced.id
                    pn.resolution_action = "matched_existing"
                    _refresh_on_reobservation(raced, proposal, proposal_node=pn)
                    _write_node_evidence(
                        session,
                        node_id=raced.id,
                        proposal=proposal,
                        proposal_node=pn,
                        merge_action="confirmed",
                    )
                    dec.node_outcomes.append(_NodeOutcome(
                        pn.id, "matched_existing", raced.id,
                        f"{pn.node_type} {pn.label!r} matched {raced.id[:8]} "
                        f"(created concurrently — race guard converted to match)",
                    ))
                    continue

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
                _mint_disambiguation_on_label_collision(session, new)
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
                    _refresh_on_reobservation(match_node, proposal, proposal_node=pn)
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
    # pod_store is the sole source of truth for pods (no kg_node mirror).
    # The FK on kg_edge_metadata.{source,target}_id → kg_node_metadata.id
    # was dropped as part of the no-mirror migration; this admission check
    # is the replacement gate. A pod URI endpoint is accepted iff:
    #   1. it exists in pod_store, AND
    #   2. its kind is `kg_admissible: true` in configs/pod_kinds.json.
    # Otherwise the proposal abandons with reason "pod_not_admissible".
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    from app.assistant.pod_store.pod_store import PodStore
    from app.assistant.pod_store import pod_kind_registry

    _pod_store_for_admission: Optional[PodStore] = None
    _pod_admission_cache: Dict[str, bool] = {}

    def _pod_uri_is_admissible(uri: str) -> bool:
        nonlocal _pod_store_for_admission
        if uri in _pod_admission_cache:
            return _pod_admission_cache[uri]
        if _pod_store_for_admission is None:
            _pod_store_for_admission = PodStore()
        pod = _pod_store_for_admission.get(uri)
        ok = bool(pod) and pod_kind_registry.is_kg_admissible(pod.kind or "")
        _pod_admission_cache[uri] = ok
        return ok

    def _resolve_endpoint(pn_id: str) -> Optional[str]:
        v = resolved_lookup.get(pn_id)
        if v is not None:
            return v
        if pn_id and POD_URI_RE.fullmatch(pn_id):
            if _pod_uri_is_admissible(pn_id):
                return pn_id
            # Pod URI not admissible — caller treats as unresolved endpoint
            # and the edge gets skipped with an explicit reason.
            return None
        return None

    def _pod_admission_reason(pn_id: str) -> Optional[str]:
        """If pn_id is a pod URI that failed admission, return why."""
        if not pn_id or not POD_URI_RE.fullmatch(pn_id):
            return None
        if pn_id in _pod_admission_cache and not _pod_admission_cache[pn_id]:
            return f"pod {pn_id[:50]} not admissible (missing or kind not kg_admissible)"
        return None

    pnodes_by_id = {pn.id: pn for pn in pnodes}

    for pe in pedges:
        src_kg = _resolve_endpoint(pe.source_node_id)
        tgt_kg = _resolve_endpoint(pe.target_node_id)
        if src_kg is None or tgt_kg is None:
            pod_reason = _pod_admission_reason(pe.source_node_id) or _pod_admission_reason(pe.target_node_id)
            if pod_reason:
                reason = pod_reason
            elif not commit:
                reason = "endpoint unresolved (new-node in dry-run)"
            else:
                reason = "endpoint unresolved (unexpected in commit mode)"
            dec.edge_outcomes.append(
                _EdgeOutcome(pe.id, "skipped_conflict", None, reason)
            )
            continue

        existing = _existing_kg_edge(
            session, src_kg, tgt_kg, pe.predicate,
            # The writer stamps bidirectional=True for symmetric phrasings;
            # honoring it here stops reversed re-assertions from minting a
            # mirror edge (the flag was previously never read — audit P2.4).
            bidirectional=bool(getattr(pe, "bidirectional", False)),
        )
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

        # Durable single-target conflict (e.g., second spouse) — temporally
        # aware since audit P2.1. The old behavior auto-`contradicted` the
        # WHOLE group forever, freezing the KG on first-asserted values for
        # exactly the facts that change in real life (job change,
        # remarriage), and `contradicted` was a verified dead end.
        conflict = _is_durable_conflict(session, src_kg, pe.predicate, tgt_kg)
        if conflict is not None:
            kind, era_node = _classify_durable_conflict(
                session, conflict, src_kg,
                _incoming_era_start(pnodes_by_id, pe),
            )
            src_node = session.get(Node, src_kg)
            src_label = src_node.label if src_node else src_kg[:8]
            old_tgt_node = session.get(Node, conflict.target_id)
            old_label = old_tgt_node.label if old_tgt_node else conflict.target_id[:8]
            new_tgt_node = session.get(Node, tgt_kg)
            new_label = new_tgt_node.label if new_tgt_node else tgt_kg[:8]

            if kind == "closed_era":
                # The existing era ended — sequential facts coexist
                # (remarriage, career history). Fall through to creation.
                logger.info(
                    "[promoter] %s conflict bypassed — existing era %r is "
                    "closed (end_date set); creating successor edge to %r",
                    pe.predicate, old_label, new_label,
                )
            elif kind == "succession":
                # Conservative path (P4.4 decision 2026-06-10): hold the
                # whole proposal and route the close-the-old-era question
                # to the user. The proposal stays 'pending', re-evaluates
                # every run, and promotes cleanly once the era is closed;
                # finding dedup stops duplicate findings meanwhile.
                old_start = era_node.start_date if era_node is not None else None
                new_start = _incoming_era_start(pnodes_by_id, pe)
                dec.followup_findings.append({
                    "finding_type": "single_target_succession",
                    "primary_node_id": src_kg,
                    "secondary_node_id": conflict.target_id,
                    "suggested_action": "review",
                    "priority": "high",
                    "confidence": 0.8,
                    "agent_name": "proposal_promoter",
                    "reason": (
                        f"Proposed {pe.predicate!r} for {src_label!r} → "
                        f"{new_label!r} postdates the still-open era "
                        f"{old_label!r} (existing start {old_start}, proposed "
                        f"start {new_start}). Likely real-life succession (job "
                        f"change / remarriage): close the old era, then the "
                        f"held proposal promotes on a later run."
                    ),
                    "evidence": {
                        "predicate": pe.predicate,
                        "proposal_id": proposal.id,
                        "era_node_id": era_node.id if era_node is not None else None,
                        "existing_start": str(old_start) if old_start else None,
                        "proposed_start": str(new_start) if new_start else None,
                        "new_target_label": new_label,
                    },
                })
                dec.final_status = "held"
                dec.error = f"succession candidate on {pe.predicate} — held for review"
                return dec
            else:  # same_era — genuine double assertion, edge granularity.
                locked = src_node is not None and src_node.locked_by_user_at is not None
                action = "skipped_locked" if locked else "skipped_conflict"
                dec.edge_outcomes.append(
                    _EdgeOutcome(pe.id, action, None,
                                 f"existing {pe.predicate!r} edge to "
                                 f"{old_label!r} conflicts with proposed {new_label!r}"
                                 + (" (source node user-locked)" if locked else "")
                                 + " — edge skipped, finding raised")
                )
                dec.followup_findings.append({
                    "finding_type": "single_target_conflict",
                    "primary_node_id": src_kg,
                    "secondary_node_id": conflict.target_id,
                    "suggested_action": "review",
                    "priority": "high",
                    "confidence": 0.8,
                    "agent_name": "proposal_promoter",
                    "reason": (
                        f"Same-era double assertion: {src_label!r} already has "
                        f"{pe.predicate!r} → {old_label!r}; proposal "
                        f"{proposal.id} asserts → {new_label!r} with no dates "
                        f"establishing an order. The conflicting edge was "
                        f"skipped; the rest of the group applied."
                        + (" Source node is user-locked." if locked else "")
                    ),
                    "evidence": {
                        "predicate": pe.predicate,
                        "proposal_id": proposal.id,
                        "new_target_label": new_label,
                        "source_locked": locked,
                    },
                })
                continue

        # Green light to create.
        if commit:
            new_edge = Edge(
                source_id=src_kg,
                target_id=tgt_kg,
                relationship_type=pe.predicate,
                sentence=pe.sentence,
                # Per-observation provenance (window_id, source message id,
                # timestamp) is in kg_edge_evidence — not denormalized here.
                # 2026-05-10 schema cleanup removed kg_edge_metadata.window_id.
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
        "held": 0,
        "findings_raised": 0,
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
                elif dec.final_status == "held":
                    # Succession candidate (audit P2.1, conservative path):
                    # roll back this proposal's partial writes and leave
                    # status='pending' DELIBERATELY — it re-evaluates every
                    # run and promotes cleanly once the old era is closed
                    # (the single_target_succession finding routes that
                    # decision to the user; upsert_finding's dedup prevents
                    # a fresh finding per run).
                    sp.rollback()
                    stats["held"] += 1
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

            # Post-commit: route succession/conflict findings through the
            # maintenance store. OUTSIDE the write transaction above —
            # upsert_finding opens its own session and SQLite is
            # single-writer. Skipped in dry-run (rolled-back work must not
            # mint real findings).
            if commit and dec.followup_findings:
                from app.assistant.kg_maintenance.store import upsert_finding
                for payload in dec.followup_findings:
                    try:
                        _, created = upsert_finding(
                            **payload, pipeline_run_id=None,
                        )
                        if created:
                            stats["findings_raised"] += 1
                    except Exception:
                        logger.exception(
                            "[promoter] failed to write %s finding for "
                            "proposal %s", payload.get("finding_type"), pid,
                        )
        except Exception:
            stats["errors"] += 1
            logger.exception("[promoter] outer handler on proposal %s", pid)

    # Tagging is NOT done here. Per the post-claim-proposal pipeline:
    # promote (NULL importance) → importance rater fills it in → section
    # tagger fires on the rated nodes → card / wiki dirty-sweep picks up
    # the new tags. Both rating and tagging happen inside
    # _lazy_kg_importance_rater so they share the same trigger and the
    # tagger sees real importance values when it runs.

    stats["_samples"] = samples
    return stats
