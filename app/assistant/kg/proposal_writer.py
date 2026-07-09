"""
Writer for the redesigned claim_proposals schema.

Called from ExtractFactsFromWindowsStep after the extractor returns its
nodes+edges for a window. Splits the output into connected subgraphs
(each becomes one claim_proposal), persists nodes/edges with proper
UUID FKs, and attaches one evidence row per component for this write.

Error-safe: exceptions are caught and logged — the legacy extraction
path remains authoritative while the proposals layer stands up.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalNode, ClaimProposalEdge,
    ClaimProposalEvidence,
)
from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import (
    KGResolvedMessage, KGWindowMessage,
)
from app.assistant.kg.claim_classifier import classify_claim_type
from app.assistant.kg.predicate_vocabulary import normalize_predicate
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Placeholder labels the extractor sometimes fabricates for things it
# couldn't resolve. These must never reach the shadow KG — let alone the
# real graph. Checked case-insensitively.
_PLACEHOLDER_LABEL_PATTERNS = [
    re.compile(r"^unknown\b", re.IGNORECASE),
    re.compile(r"^unspecified\b", re.IGNORECASE),
    re.compile(r"^unnamed\b", re.IGNORECASE),
    re.compile(r"^unidentified\b", re.IGNORECASE),
    re.compile(r"\(unknown\)", re.IGNORECASE),
]


def _is_placeholder_label(label: str) -> bool:
    s = (label or "").strip()
    if not s:
        return True  # empty label is also not acceptable
    for pat in _PLACEHOLDER_LABEL_PATTERNS:
        if pat.search(s):
            return True
    return False


def _enrich_nodes_via_meta_data_add(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    anchor: Dict[str, Any],
    stats: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Call meta_data_add agent to fill in dates/aliases/confidence on
    the extractor's bare nodes. Merges enriched fields into each node by
    temp_id. Error-safe: if the agent fails, returns the original nodes
    unchanged so the proposal still gets written (just without dates).
    """
    if not nodes:
        return nodes

    try:
        import json as _json
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.scope.loader import load_scope_for_source
        from app.assistant.utils.pydantic_classes import Message
    except Exception as exc:
        logger.warning("[proposal_writer] meta_data_add unavailable: %s", exc)
        return nodes

    agent = DI.agent_factory.create_agent("knowledge_graph_add::meta_data_add")
    if agent is None:
        logger.warning("[proposal_writer] could not create meta_data_add agent")
        return nodes

    # Build the input shape meta_data_add expects.
    valid_nodes = [n for n in nodes if n.get("temp_id")]
    if not valid_nodes:
        return nodes

    sentences = [n.get("sentence") or "" for n in valid_nodes]
    combined_sentence = " | ".join(dict.fromkeys(s for s in sentences if s))
    if not combined_sentence:
        # Fall back to edge sentences for temporal context.
        edge_sents = [e.get("sentence") or "" for e in (edges or [])]
        combined_sentence = " | ".join(dict.fromkeys(s for s in edge_sents if s))

    node_payloads = [
        {
            "temp_id": n.get("temp_id"),
            "node_type": n.get("node_type"),
            "label": n.get("label"),
            "category": n.get("category"),
            "sentence": n.get("sentence"),
        }
        for n in valid_nodes
    ]

    observed_at = anchor.get("observed_at")
    message_ts = observed_at.isoformat() if observed_at else ""

    agent_input = {
        "nodes": _json.dumps(node_payloads, ensure_ascii=True),
        "resolved_sentence": combined_sentence,
        "message_timestamp": message_ts,
    }

    scope = load_scope_for_source(
        kind="pipeline", source_id="kg_pipeline", actor_id="proposal_writer",
    )
    try:
        resp = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = resp.data if resp and hasattr(resp, "data") else {}
    except Exception as exc:
        logger.warning("[proposal_writer] meta_data_add call failed: %s", exc)
        return nodes

    meta_nodes = data.get("Nodes") or [] if isinstance(data, dict) else []
    meta_by_temp: Dict[str, Dict[str, Any]] = {}
    for mn in meta_nodes:
        if isinstance(mn, dict) and mn.get("temp_id"):
            meta_by_temp[str(mn["temp_id"])] = mn

    if not meta_by_temp:
        return nodes

    # Merge enrichment into each extractor node. Only overwrite if the
    # enriched value is non-empty — extractor's raw label/category stay
    # authoritative; enrichment adds the temporal and quality fields.
    enriched_any = False
    merged: List[Dict[str, Any]] = []
    for n in nodes:
        tid = str(n.get("temp_id") or "")
        meta = meta_by_temp.get(tid)
        if not meta:
            merged.append(n)
            continue
        combined = dict(n)  # copy
        for fld in (
            "aliases", "hash_tags", "start_date", "start_date_confidence",
            "start_date_prose", "end_date", "end_date_confidence",
            "end_date_prose", "valid_during", "semantic_label", "goal_status",
            "confidence", "importance",
        ):
            val = meta.get(fld)
            if val not in (None, "", []):
                combined[fld] = val
        merged.append(combined)
        enriched_any = True

    if enriched_any:
        stats["enriched"] = stats.get("enriched", 0) + len(meta_by_temp)
    return merged


def _fetch_window_anchor(session, window_id: str) -> Dict[str, Any]:
    """Pull the chat-context envelope for the first user-role message
    in this window. Used as the provenance anchor for every proposal
    derived from the window.

    Walks the unified pipeline schema:
      kg_window_message -> unified_log_2026 (verbatim text + speaker)
                        -> kg_resolved_message (resolved text, optional)
    """
    if not window_id:
        return {}
    rows = (
        session.query(KGWindowMessage, UnifiedLog2026)
        .join(UnifiedLog2026, UnifiedLog2026.id == KGWindowMessage.unified_log_id)
        .filter(KGWindowMessage.window_id == window_id)
        .order_by(KGWindowMessage.item_order.asc())
        .all()
    )
    if not rows:
        return {}
    anchor_row = next(
        ((wm, ul) for wm, ul in rows if (ul.role or "").lower() == "user"),
        rows[0],
    )
    _wm, ul = anchor_row
    return {
        "unified_log_id": ul.id,
        "room_id": ul.room_id,
        "speaker_name": ul.speaker_name,
        "speaker_role": ul.role,
        "source_value": ul.source,
        "observed_at": ul.timestamp,
    }


def _already_written_for_window(session, window_id: str) -> bool:
    """Idempotency check: did a prior extraction of this window already
    produce proposals? If so, skip — the caller shouldn't dual-commit.
    """
    if not window_id:
        return False
    hit = (
        session.query(ClaimProposalEvidence.id)
        .filter(ClaimProposalEvidence.window_id == window_id)
        .first()
    )
    return hit is not None


def _representative_sentence(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    anchor_speaker_name: Optional[str] = None,
) -> Optional[str]:
    """Pick a representative sentence for the proposal group.

    Heuristic: prefer edges whose source is the protagonist (the chat's
    speaker, typically "Jukka"). That's usually the thesis edge — e.g.,
    "Jukka has_goal → Walk Dogs" is a better group-summary than
    "Walk Dogs targets → Bonnie".

    Falls back to the first-edge-sentence if no protagonist match.
    """
    nodes_by_temp = {str(n.get("temp_id")): n for n in nodes if n.get("temp_id")}

    def _label_of(temp_id) -> str:
        n = nodes_by_temp.get(str(temp_id) if temp_id is not None else "")
        return (n.get("label") if n else "") or ""

    speaker = (anchor_speaker_name or "").strip().lower()

    # Pass 1: edges whose source label matches the anchor speaker.
    if speaker:
        for e in edges:
            src_label = _label_of(e.get("source") or e.get("source_temp_id")).lower()
            if src_label == speaker:
                sent = (e.get("sentence") or "").strip()
                if sent:
                    return sent

    # Pass 2: first non-empty edge sentence.
    for e in edges:
        sent = (e.get("sentence") or "").strip()
        if sent:
            return sent

    return None


def _derive_claim_type(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    anchor: Dict[str, Any],
) -> str:
    """Classify the group's claim type. Looks at the group's predicates
    and the source's speaker role. If multiple edges with different
    classifications exist, pick the most-durable (durable > declarative
    > ephemeral > inferred > unknown)."""
    if not edges:
        return "unknown"

    ranks = {
        "durable": 4,
        "declarative_singular": 3,
        "ephemeral": 2,
        "inferred": 1,
        "unknown": 0,
    }
    best = "unknown"
    for e in edges:
        canonical_pred, _ = normalize_predicate(
            e.get("relationship_type") or e.get("label") or ""
        )
        ct = classify_claim_type({
            "source_type": "chat",
            "speaker_role": anchor.get("speaker_role"),
            "predicate": canonical_pred,
        })
        if ranks[ct] > ranks[best]:
            best = ct
    return best


def _parse_ts(value) -> Optional[datetime]:
    parsed, _ = _parse_ts_or_prose(value)
    return parsed


def _parse_ts_or_prose(value) -> tuple[Optional[datetime], Optional[str]]:
    """Parse an ISO timestamp; an unparseable NON-EMPTY string comes back as
    PROSE instead of vanishing. The extractor legitimately emits partial
    dates ("2024-08", "spring 2023") that fromisoformat rejects — these
    used to be swallowed to None with no trace (audit G3), losing the
    node's temporal anchor. The prose pipeline (valid_*_prose ->
    Node.start/end_date_prose -> the promoter's year-mismatch filter)
    already exists downstream; this routes into it.
    """
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value, None
    s = str(value).strip()
    if not s:
        return None, None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")), None
    except Exception:
        logger.warning(
            "[proposal_writer] unparseable date %r — keeping it as date prose",
            s,
        )
        return None, s


def write_one_proposal_group(
    session,
    *,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    anchor: Dict[str, Any],
    window_id: Optional[str] = None,
    projection_id: Optional[str] = None,
    enrichment_id: Optional[str] = None,
    extractor_agent_name: str = "fact_extractor",
    extractor_model: Optional[str] = None,
    extractor_prompt_version: Optional[str] = None,
    extraction_run_id: Optional[str] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """Persist one connected-component group as a single claim proposal.

    Caller responsibilities:
      - Pass already-split (single-component) nodes/edges.
      - Pass already-enriched nodes (this helper does NOT call meta_data_add).
      - Provide ``anchor``: ``{room_id, speaker_name, speaker_role,
        unified_log_id, observed_at, raw_text?}``.
      - Provide a ``window_id`` (the conversation chunk that produced this
        proposal). Legacy chat pipeline points at kg_chat_conversation_window;
        new kg_pipeline points at kg_window — both are conceptually windows.
        Optionally pass ``projection_id`` (legacy) or ``enrichment_id`` (new)
        for richer provenance.
      - Manage the surrounding transaction.

    Writes (with FK-safe flush ordering): ClaimProposal → ClaimProposalNode(s)
    → ClaimProposalEdge(s) → ClaimProposalEvidence.

    Returns the new ``proposal_id``, or ``None`` if the group was skipped
    because it contained placeholder-label nodes.
    """
    if stats is None:
        stats = {}

    placeholders = [
        n.get("label") for n in nodes
        if _is_placeholder_label(n.get("label") or "")
    ]
    if placeholders:
        logger.warning(
            "[proposal_writer] skipping group: placeholder labels %r "
            "(window=%s)", placeholders, window_id,
        )
        stats["groups_skipped_placeholder"] = stats.get("groups_skipped_placeholder", 0) + 1
        return None

    claim_type = _derive_claim_type(nodes, edges, anchor)
    rep_sentence = _representative_sentence(nodes, edges, anchor.get("speaker_name"))

    observed_at = anchor.get("observed_at")
    observed_iso: Optional[str] = None
    if observed_at is not None:
        observed_iso = (
            observed_at.isoformat() if hasattr(observed_at, "isoformat")
            else str(observed_at)
        )

    proposal = ClaimProposal(
        status="pending",
        claim_type=claim_type or "unknown",
        representative_sentence=rep_sentence,
        observation_count=1,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    )
    session.add(proposal)
    session.flush()  # parent before children for FK
    stats["groups_written"] = stats.get("groups_written", 0) + 1

    temp_to_uuid: Dict[str, str] = {}
    for n in nodes:
        extractor_tid = str(n.get("temp_id") or "")
        label = ((n.get("label") or "").strip())[:512] or "(unknown)"
        node_type = (n.get("node_type") or "Entity").strip() or "Entity"

        # first_observed no longer stored in attributes — it's a first-class
        # column on Node since 2026-05-11, derived from proposal_node.valid_from
        # at promotion time. Promoter handles the column write directly.
        extractor_attrs = n.get("attributes") or {}
        merged_attrs = dict(extractor_attrs) if isinstance(extractor_attrs, dict) else {}
        # Carry enrichment fields that the promoter pops back out to first-class
        # columns. semantic_label / goal_status / confidence / valid_during all
        # have first-class columns and the promoter pops them. importance was
        # removed from meta_data_add 2026-05-11 — dedicated rater is the source.
        for fld in ("semantic_label", "goal_status", "confidence", "valid_during"):
            val = n.get(fld)
            if val not in (None, "", []):
                merged_attrs[fld] = val

        category = (n.get("category") or "")
        category = category[:128] if category else None

        vf_parsed, vf_leftover = _parse_ts_or_prose(n.get("valid_from") or n.get("start_date"))
        vt_parsed, vt_leftover = _parse_ts_or_prose(n.get("valid_to") or n.get("end_date"))

        row = ClaimProposalNode(
            proposal_id=proposal.id,
            extractor_temp_id=extractor_tid or None,
            label=label,
            node_type=node_type,
            category=category,
            sentence=n.get("sentence") or n.get("original_sentence") or None,
            description_draft=n.get("description") or n.get("description_draft"),
            attributes_json=merged_attrs or None,
            valid_from=vf_parsed,
            valid_to=vt_parsed,
            valid_from_prose=n.get("valid_from_prose") or n.get("start_date_prose") or vf_leftover,
            valid_to_prose=n.get("valid_to_prose") or n.get("end_date_prose") or vt_leftover,
            start_date_confidence=n.get("start_date_confidence"),
            end_date_confidence=n.get("end_date_confidence"),
            valid_currently=n.get("valid_currently"),
            validity_reason=n.get("validity_reason") or None,
        )
        session.add(row)
        # Flush every node so its UUID PK exists before edges reference it.
        # Without this, SQLAlchemy 2.x's batched insertmany may try to write
        # edges before nodes hit the table → FOREIGN KEY constraint failed.
        session.flush()
        if extractor_tid:
            temp_to_uuid[extractor_tid] = row.id
        stats["nodes_written"] = stats.get("nodes_written", 0) + 1

    # Pod URIs (e.g., datapod:image:abc...) are KG references, not kg_nodes.
    # The fact_extractor emits them verbatim as edge endpoints when an
    # attached image is the subject. We do NOT mint a claim_proposal_node
    # placeholder for them — the FK on claim_proposal_edge.{source,target}
    # _node_id was dropped as part of the no-mirror migration. The promoter
    # validates pod URIs against pod_store at admission time.
    #
    # All we need here is to add the URI to temp_to_uuid so the edge writer
    # threads the URI through as the resolved endpoint.
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    pod_uris_seen: set[str] = set()
    for e in edges:
        for tid in (
            str(e.get("source") or e.get("source_temp_id") or ""),
            str(e.get("target") or e.get("target_temp_id") or ""),
        ):
            if not tid or not POD_URI_RE.fullmatch(tid) or tid in pod_uris_seen:
                continue
            pod_uris_seen.add(tid)
            temp_to_uuid[tid] = tid

    for e in edges:
        src_tid = str(e.get("source") or e.get("source_temp_id") or "")
        tgt_tid = str(e.get("target") or e.get("target_temp_id") or "")
        src_uuid = temp_to_uuid.get(src_tid)
        tgt_uuid = temp_to_uuid.get(tgt_tid)
        if not src_uuid or not tgt_uuid:
            logger.warning(
                "[proposal_writer] edge endpoint missing temp_id mapping: "
                "src=%r tgt=%r (proposal %s, window=%s) — skipping edge",
                src_tid, tgt_tid, proposal.id, window_id,
            )
            continue
        raw_predicate = e.get("relationship_type") or e.get("label") or ""
        canonical_pred, _ = normalize_predicate(raw_predicate)
        session.add(ClaimProposalEdge(
            proposal_id=proposal.id,
            source_node_id=src_uuid,
            target_node_id=tgt_uuid,
            predicate=(canonical_pred or "related_to")[:128],
            bidirectional=bool(e.get("bidirectional", False)),
            sentence=e.get("sentence"),
        ))
        stats["edges_written"] = stats.get("edges_written", 0) + 1

    # `unified_log_id` is deliberately NOT set. The anchor's unified_log_id
    # was the FIRST user message in the window — not the actual source of any
    # particular claim. Multi-topic windows produced misattributions that
    # downstream consumers silently treated as truth. Provenance is now
    # window-level only: walk window_id -> kg_window_message -> unified_log_2026
    # to see all messages that produced this proposal's facts.
    session.add(ClaimProposalEvidence(
        proposal_id=proposal.id,
        source_type="chat",
        room_id=anchor.get("room_id"),
        room_surface=None,  # not in kg_chat_projection today
        speaker_name=anchor.get("speaker_name"),
        speaker_role=anchor.get("speaker_role"),
        window_id=window_id,
        projection_id=projection_id,
        enrichment_id=enrichment_id,
        unified_log_id=None,
        raw_text=anchor.get("raw_text") or rep_sentence,
        extractor_agent_name=extractor_agent_name,
        extractor_model=extractor_model,
        extractor_prompt_version=extractor_prompt_version,
        extraction_run_id=extraction_run_id,
        observed_at=observed_at,
    ))
    stats["evidence_written"] = stats.get("evidence_written", 0) + 1

    return proposal.id


