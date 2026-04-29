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

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalNode, ClaimProposalEdge,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

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
    """Match an Entity/Concept/Goal/Property node by canonical label or alias.

    Prefers locked + higher pagerank when multiple match.
    """
    if not label:
        return None
    label_lower = label.lower().strip()
    if not label_lower:
        return None

    hit = (
        session.query(Node)
        .filter(func.lower(Node.label) == label_lower)
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )
    if hit:
        return hit

    like_pat = f'%"{label_lower}"%'
    return (
        session.query(Node)
        .filter(func.lower(func.coalesce(func.cast(Node.aliases, type_=__import__("sqlalchemy").String), "")).like(like_pat))
        .order_by(Node.locked_by_user_at.desc().nulls_last(),
                  Node.pagerank_score.desc().nulls_last())
        .first()
    )


_EVENT_DATE_TOLERANCE_DAYS = 7  # events more than this many days apart → different instances


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


def _resolve_state_event(
    session,
    proposal_node: ClaimProposalNode,
    participant_kg_ids: List[str],
    *,
    proposal_window_id: Optional[str] = None,
) -> Optional[Node]:
    """Decide whether this proposal State/Event node refers to an existing
    one in the KG.

    Three-stage, with NO auto-merge on surface metadata:
      1. Deterministic candidate prune: same node_type + participant
         overlap ≥ 1. Dates and labels are NOT used here because extractor
         labels are LLM-generated and dates are often NULL on legacy rows.
      2. Deterministic EXCLUSION only (shrinks the LLM pool, never merges):
           - Event-class with known disparate start_dates (>N days apart)
             → definitely different instances, drop the candidate.
      3. LLM judgment on every surviving candidate, with enriched context
         (original_sentence, window_ids, participant labels, TTL, dates).

    Why no auto-merge:
      Label + participants + date + window can't tell "same claim extracted
      twice" from "two claims in one window" — morning-walk vs afternoon-
      walk, two beliefs in the same utterance, recurring events. That
      distinguishing signal lives in the source text. Wrong-merge is
      silent corruption; wrong-split is a duplicate the weekly maintenance
      scan catches. The asymmetry mandates always calling the LLM.
    """
    if not participant_kg_ids:
        return None

    participant_set = set(participant_kg_ids)

    # Pre-filter candidates via SQL: State/Event nodes that share at least
    # one participant edge with our proposal.
    candidate_ids_q = (
        session.query(Edge.source_id, Edge.target_id)
        .filter(
            ((Edge.source_id.in_(participant_set)) | (Edge.target_id.in_(participant_set))),
        )
    )
    connected_node_ids: set = set()
    for src, tgt in candidate_ids_q.all():
        if src in participant_set and tgt not in participant_set:
            connected_node_ids.add(tgt)
        elif tgt in participant_set and src not in participant_set:
            connected_node_ids.add(src)

    if not connected_node_ids:
        return None

    # Same node_type + connected via at least one participant. No start_date
    # filter here — NULL start_dates on legacy candidates used to slip
    # through this as mismatches; we handle date compatibility in Stage 2.
    candidates = (
        session.query(Node)
        .filter(Node.node_type == proposal_node.node_type)
        .filter(Node.id.in_(connected_node_ids))
        .all()
    )
    if not candidates:
        return None

    # Score by participant overlap (Jaccard). Drop zero-overlap leftovers
    # that slipped the SQL pre-filter on a false-positive edge.
    scored: List[Dict[str, Any]] = []
    for cand in candidates:
        cand_parts = _get_participant_kg_ids(session, cand.id)
        if not cand_parts:
            continue
        inter = cand_parts & participant_set
        union = cand_parts | participant_set
        if not inter:
            continue
        scored.append({
            "node": cand,
            "cand_participants": cand_parts,
            "overlap": len(inter),
            "jaccard": len(inter) / max(1, len(union)),
        })
    if not scored:
        return None
    scored.sort(key=lambda s: (s["jaccard"], s["overlap"]), reverse=True)

    # Deliberately no deterministic auto-merge for States/Events/Goals.
    # Surface metadata (label + participants + date + window) can't
    # distinguish "same claim extracted twice" from "two claims in one
    # window" — morning-walk vs afternoon-walk, two beliefs stated
    # together, recurring events. That signal lives in the source text
    # and requires the LLM. We route every surviving candidate through
    # node_merger. Wrong-merge is silent corruption; wrong-split is a
    # duplicate the weekly maintenance scan recovers. The asymmetry
    # dictates: never auto-merge on metadata alone.

    # Stage 2: Event-class disparate-date exclusion. If both proposal and
    # candidate have known start_dates more than N days apart, treat as
    # distinct instances. States have no such exclusion — identity-class
    # states (Marriage, Residence, Ownership) persist across time.
    if (proposal_node.node_type or "").lower() == "event" and proposal_node.valid_from is not None:
        pv = proposal_node.valid_from
        kept: List[Dict[str, Any]] = []
        for s in scored:
            cs = s["node"].start_date
            if cs is None:
                kept.append(s)  # unknown date; let LLM decide
                continue
            try:
                delta = abs((cs.date() - pv.date()).days)
            except Exception:
                kept.append(s)
                continue
            if delta <= _EVENT_DATE_TOLERANCE_DAYS:
                kept.append(s)
            # else: different days, drop — definitely different events
        scored = kept
    if not scored:
        return None

    # Top candidates — cap the LLM input to avoid context bloat on hub
    # participants (e.g. nodes with Jukka in them are legion).
    top = scored[:5]
    top_candidates = [s["node"] for s in top]

    # Stage 3: LLM judgment with enriched context. The earlier exact-label
    # fast-path was removed because it over-merged recurring events
    # (Walking + Walking on different days). Let the LLM weigh label +
    # participants + time + window together.
    return _ask_node_merger_for_state_match(
        session,
        proposal_node,
        top_candidates,
        proposal_window_id=proposal_window_id,
    )


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
            sql_text("SELECT end_unified_timestamp FROM kg_chat_conversation_window WHERE id = :w"),
            {"w": window_id},
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return None


def _ask_node_merger_for_state_match(
    session,
    proposal_node: ClaimProposalNode,
    candidates: List[Node],
    *,
    proposal_window_id: Optional[str] = None,
) -> Optional[Node]:
    """Call node_merger with rich context for a State/Event merge decision.

    Context includes both the proposal and each candidate's:
      - label, node_type, category, description, original_sentence
      - valid_from / valid_to (structured + prose)
      - first_observed (from attributes)
      - source window id + window end timestamp
      - participant labels (not just ids)
      - TTL duration class (ephemeral vs long_term vs durable)

    Hard merge decisions depend on the LLM seeing all of this at once.
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

    proposal_attrs = proposal_node.attributes_json or {}
    if not isinstance(proposal_attrs, dict):
        proposal_attrs = {}
    proposal_ttl = proposal_attrs.get("ttl") if isinstance(proposal_attrs, dict) else None

    new_node_ctx = {
        "label": proposal_node.label,
        "node_type": proposal_node.node_type,
        "category": proposal_node.category,
        "description": proposal_node.description_draft,
        "original_sentence": getattr(proposal_node, "sentence", None),
        "valid_from": proposal_node.valid_from.isoformat() if proposal_node.valid_from else None,
        "valid_to": proposal_node.valid_to.isoformat() if proposal_node.valid_to else None,
        "valid_from_prose": proposal_node.valid_from_prose,
        "valid_to_prose": proposal_node.valid_to_prose,
        "source_window_id": proposal_window_id,
        "source_window_end_ts": _window_end_ts(session, proposal_window_id),
        "first_observed": proposal_attrs.get("first_observed") if isinstance(proposal_attrs, dict) else None,
        "ttl_duration_class": (proposal_ttl.get("duration_class") if isinstance(proposal_ttl, dict) else None),
        # Participants for the proposal come from its sibling proposal_edges;
        # we don't have them here without a round-trip. Label-only context.
    }

    cand_payload = []
    for cand in candidates:
        cand_attrs = cand.attributes or {}
        if not isinstance(cand_attrs, dict):
            cand_attrs = {}
        cand_ttl = cand_attrs.get("ttl") if isinstance(cand_attrs, dict) else None
        cand_window = _first_window_id_for_node(session, cand.id)
        cand_payload.append({
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
            "participant_labels": _participant_labels(session, cand.id),
        })

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="proposal_promoter",
    )
    agent_input = {
        "new_node_context": json.dumps(new_node_ctx, ensure_ascii=True, indent=2),
        "existing_node_candidates": json.dumps(cand_payload, ensure_ascii=True, indent=2),
    }
    try:
        resp = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = resp.data if resp and hasattr(resp, "data") else {}
    except Exception as exc:
        logger.warning("[promoter] node_merger call failed for state %r: %s",
                       proposal_node.label, exc)
        return None

    if not bool(data.get("merge_nodes")):
        return None
    merged_id = str(data.get("merged_node_id") or "").strip()
    if not merged_id:
        return None
    cand_by_id = {c.id: c for c in candidates}
    chosen = cand_by_id.get(merged_id)
    if chosen is None:
        logger.warning(
            "[promoter] node_merger returned id %s not in candidate set",
            merged_id,
        )
        return None
    logger.info(
        "[promoter] LLM merged state %r → %s (%s): %s",
        proposal_node.label, chosen.id[:8], chosen.label,
        (data.get("reasoning") or "")[:120],
    )
    return chosen


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


def _participants_of_proposal_state(
    proposal_nodes: List[ClaimProposalNode],
    proposal_edges: List[ClaimProposalEdge],
    state_node_id: str,
) -> List[str]:
    """Return KG ids of participant nodes attached to this state/event
    within its proposal group. Participants are the entity-like proposal
    nodes connected to the state via any edge."""
    nodes_by_id = {n.id: n for n in proposal_nodes}
    participant_uuids: Set[str] = set()
    for e in proposal_edges:
        if e.source_node_id == state_node_id:
            other = e.target_node_id
        elif e.target_node_id == state_node_id:
            other = e.source_node_id
        else:
            continue
        cn = nodes_by_id.get(other)
        if cn and cn.node_type in ENTITY_LIKE_TYPES and cn.resolved_node_id:
            participant_uuids.add(cn.resolved_node_id)
    return list(participant_uuids)


# ---------------------------------------------------------------------------
# Node / edge creation
# ---------------------------------------------------------------------------

def _create_kg_node_from_proposal(
    session, proposal_node: ClaimProposalNode, proposal_id: str,
    *,
    originating_sentence: Optional[str] = None,
    participant_labels: Optional[List[str]] = None,
) -> Node:
    """Create a fresh kg_node_metadata row from this proposal_node.

    For State and Event nodes, ask the ``state_ttl_estimator`` agent for a
    duration estimate and stash it in ``attributes.ttl`` so the nightly
    decay job can auto-close the era when it expires.

    For State/Event/Goal nodes, ask the ``fact_canonicalizer`` agent to
    rewrite the extractor sentence into its present-tense canonical form
    before storing it as ``Node.original_sentence``. This pairs cleanly with
    the validity dates: the sentence is the proposition; the dates bound
    when it's true. Verbatim source stays recoverable via window_id +
    claim_proposal_evidence.
    """
    attrs = dict(proposal_node.attributes_json or {}) if isinstance(proposal_node.attributes_json, dict) else {}

    # TTL estimation for State/Event nodes only. Entity/Concept/Goal/Property
    # nodes don't decay — their identity is timeless.
    if proposal_node.node_type in RELATIONSHIP_LIKE_TYPES:
        ttl = _estimate_state_ttl(
            proposal_node,
            originating_sentence=originating_sentence,
            participant_labels=participant_labels,
        )
        if ttl is not None:
            attrs["ttl"] = ttl  # {duration_class, estimated_duration_days, confidence, reasoning}

    # Canonicalize for State/Event/Goal — produces the present-tense
    # proposition that downstream agents read as the node's claim. Falls
    # back to the raw extractor sentence on failure.
    canonical_sentence = (proposal_node.sentence or "") if hasattr(proposal_node, "sentence") else ""
    if proposal_node.node_type in {"State", "Event", "Goal"}:
        rewritten = _canonicalize_sentence(
            proposal_node, participant_labels=participant_labels,
        )
        if rewritten:
            canonical_sentence = rewritten

    new = Node(
        label=proposal_node.label,
        node_type=proposal_node.node_type,
        # Present-tense canonical for State/Event/Goal (via fact_canonicalizer);
        # raw extractor sentence for Entity/Concept/Property. Verbatim source
        # is preserved in evidence + window_id, not on the node.
        original_sentence=canonical_sentence,
        description=proposal_node.description_draft or "",
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


def _write_edge_evidence_for_reinforcement(
    session, edge_id: str, proposal: ClaimProposal, proposal_edge: ClaimProposalEdge,
) -> None:
    """When promoter matches a proposal_edge to an existing KG edge, append
    a kg_edge_evidence row so the graph's own provenance log records the
    reinforcement. Without this, the edge doesn't know about proposals that
    re-observed it — evidence only lives on the shadow layer.
    """
    try:
        from app.assistant.database.kg_chat_projection import KGEdgeEvidence, KGChatConversationWindow
        from app.assistant.database.claim_proposals import ClaimProposalEvidence
    except Exception as exc:
        logger.warning("[promoter] evidence cascade unavailable: %s", exc)
        return

    # Pick the earliest evidence row on this proposal as the observation source.
    ev = (
        session.query(ClaimProposalEvidence)
        .filter(ClaimProposalEvidence.proposal_id == proposal.id)
        .order_by(ClaimProposalEvidence.observed_at.asc())
        .first()
    )
    session.add(KGEdgeEvidence(
        edge_id=edge_id,
        source_table="claim_proposal",
        source_id=proposal.id,
        source_text=(ev.raw_text if ev else None),
        derived_sentence=proposal_edge.sentence,
        message_timestamp=(ev.observed_at if ev else None),
        window_id=(ev.window_id if ev else None),
        merge_action="confirmed",
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
# Core: evaluate one proposal
# ---------------------------------------------------------------------------

def _evaluate_and_apply(
    session, proposal: ClaimProposal, *, commit: bool,
) -> _ProposalDecision:
    """Walk one proposal group — resolve/create nodes, then create edges."""
    dec = _ProposalDecision(proposal.id)
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

    # Reject outright if any placeholder labels snuck through.
    placeholders = [pn.label for pn in pnodes if _is_placeholder_label(pn.label)]
    if placeholders:
        dec.final_status = "contradicted"
        dec.error = f"placeholder labels present: {placeholders}"
        for pn in pnodes:
            dec.node_outcomes.append(
                _NodeOutcome(pn.id, "skipped_locked",
                             reason=f"group rejected (placeholder labels: {placeholders})")
            )
        return dec

    # ----- Nodes: entity-like first, then relationship-like -----
    # Entity-like match by label; relationship-like needs their participants
    # resolved first to use participant-based matching.

    # Phase 1a: entity-like nodes.
    for pn in pnodes:
        if pn.node_type not in ENTITY_LIKE_TYPES:
            continue
        match = _resolve_entity_like(session, pn.label)
        if match is not None:
            pn.resolved_node_id = match.id
            pn.resolution_action = "matched_existing"
            if commit:
                _refresh_on_reobservation(match, proposal)
            dec.node_outcomes.append(
                _NodeOutcome(pn.id, "matched_existing", match.id,
                             f"{pn.node_type} {pn.label!r} matched {match.id[:8]}")
            )
        else:
            if commit:
                new = _create_kg_node_from_proposal(session, pn, proposal.id)
                pn.resolved_node_id = new.id
                pn.resolution_action = "created_new"
                dec.node_outcomes.append(
                    _NodeOutcome(pn.id, "created_new", new.id,
                                 f"created {pn.node_type} {pn.label!r} as {new.id[:8]}")
                )
            else:
                # dry-run: pretend we'd create
                dec.node_outcomes.append(
                    _NodeOutcome(pn.id, "created_new", None,
                                 f"(dry-run) would create {pn.node_type} {pn.label!r}")
                )

    # Phase 1b: relationship-like nodes (match by participants+valid_from).
    # Collect labels of all entity-like nodes in this group once; used as
    # the participant-list context for TTL estimation.
    group_entity_labels = [
        p.label for p in pnodes if p.node_type in ENTITY_LIKE_TYPES and p.label
    ]
    # Best-effort originating sentence for TTL context: proposal's rep sentence.
    originating_sentence = proposal.representative_sentence or ""

    # Resolve once: the proposal's source window id (used by
    # _resolve_state_event for same-window deterministic merge).
    proposal_window_id = _first_window_id_for_proposal(session, proposal.id)

    for pn in pnodes:
        if pn.node_type not in RELATIONSHIP_LIKE_TYPES:
            continue
        participant_kg_ids = _participants_of_proposal_state(pnodes, pedges, pn.id)
        match = _resolve_state_event(
            session, pn, participant_kg_ids,
            proposal_window_id=proposal_window_id,
        ) if participant_kg_ids else None
        if match is not None:
            pn.resolved_node_id = match.id
            pn.resolution_action = "matched_existing"
            if commit:
                _refresh_on_reobservation(match, proposal)
            dec.node_outcomes.append(
                _NodeOutcome(pn.id, "matched_existing", match.id,
                             f"{pn.node_type} {pn.label!r} matched {match.id[:8]} "
                             f"via participants")
            )
        else:
            if commit:
                new = _create_kg_node_from_proposal(
                    session, pn, proposal.id,
                    originating_sentence=originating_sentence,
                    participant_labels=group_entity_labels,
                )
                pn.resolved_node_id = new.id
                pn.resolution_action = "created_new"
                ttl_blurb = ""
                if isinstance(new.attributes, dict) and "ttl" in new.attributes:
                    t = new.attributes["ttl"]
                    ttl_blurb = f" [ttl: {t.get('duration_class')}={t.get('estimated_duration_days')}d, conf={t.get('confidence'):.2f}]"
                dec.node_outcomes.append(
                    _NodeOutcome(pn.id, "created_new", new.id,
                                 f"created {pn.node_type} {pn.label!r} as {new.id[:8]}{ttl_blurb}")
                )
            else:
                dec.node_outcomes.append(
                    _NodeOutcome(pn.id, "created_new", None,
                                 f"(dry-run) would create {pn.node_type} {pn.label!r}")
                )

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
                _write_edge_evidence_for_reinforcement(
                    session, existing.id, proposal, pe,
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

    Returns a stats dict + a small sample of per-proposal decisions.
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

    session = get_session()
    try:
        pending = (
            session.query(ClaimProposal)
            .filter(ClaimProposal.status == "pending")
            .order_by(ClaimProposal.created_at.asc())
            .limit(limit)
            .all()
        )
        for p in pending:
            try:
                # Each proposal gets its own SAVEPOINT via begin_nested.
                session.begin_nested()
                try:
                    dec = _evaluate_and_apply(session, p, commit=commit)
                except Exception as inner:
                    session.rollback()
                    stats["errors"] += 1
                    logger.exception("[promoter] proposal %s threw: %s", p.id, inner)
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
                    session.rollback()
                    if commit:
                        p.status = "contradicted"
                        p.retraction_reason = dec.error or "conflict"
                    stats["contradicted"] += 1
                else:
                    if commit:
                        p.status = "promoted"
                    stats["promoted"] += 1

                if len(samples) < 10:
                    samples.append(dec.summary())
            except Exception:
                stats["errors"] += 1
                logger.exception("[promoter] outer handler on proposal %s", p.id)

        if commit:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()

    stats["_samples"] = samples
    return stats
