import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import func

from app.assistant.embeddings.embedder import embed_text as _embed_text
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def normalize_iso_datetime(dt_val: Any):
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        return dt_val
    if isinstance(dt_val, str):
        s = dt_val.strip().lower()
        if s in {"", "unknown", "null", "none", ":null", ":null,", "/null", "_null"}:
            return None
        try:
            if s.endswith("z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception as exc:
            logger.debug("normalize_iso_datetime: could not parse %r: %s", dt_val, exc)
            return None
    return None


def find_node_candidates_exact(session, label: str, node_type: str, limit: int = 5) -> List[Node]:
    if not label:
        return []
    query = session.query(Node).filter(func.lower(Node.label) == label.lower())
    if node_type:
        query = query.filter(Node.node_type == node_type)
    return query.order_by(Node.created_at.desc()).limit(limit).all()


_embedding_cache: Dict[str, List[float]] = {}


def create_embedding(text: str) -> List[float]:
    key = (text or "").strip().lower()
    if not key:
        return []
    if key in _embedding_cache:
        return _embedding_cache[key]
    emb = _embed_text(text)
    _embedding_cache[key] = emb
    return emb


def cosine_similarity(vec1, vec2) -> float:
    if vec1 is None or vec2 is None:
        return 0.0
    v1 = np.array(vec1)
    v2 = np.array(vec2)
    if v1.size == 0 or v2.size == 0:
        return 0.0
    denom = np.linalg.norm(v1) * np.linalg.norm(v2)
    if denom == 0:
        return 0.0
    return float(np.dot(v1, v2) / denom)


def _find_exact_match_nodes(session, label: str, node_type: Optional[str] = None) -> List[Node]:
    q = session.query(Node).filter(Node.label == label)
    if node_type:
        q = q.filter(Node.node_type == node_type)
    return q.order_by(Node.created_at.desc()).all()


def _find_case_insensitive_exact_match_nodes(session, label: str, node_type: Optional[str] = None) -> List[Node]:
    q = session.query(Node).filter(func.lower(Node.label) == label.lower())
    if node_type:
        q = q.filter(Node.node_type == node_type)
    return q.order_by(Node.created_at.desc()).all()


def _find_nodes_by_alias(session, alias: str, node_type: Optional[str] = None) -> List[Node]:
    # .contains([x]) on SQLite JSON columns generates LIKE '%["x"]%' which
    # only matches single-element arrays. Use quoted-string LIKE instead.
    if not alias:
        return []
    escaped = alias.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f'%"{escaped}"%'
    q = session.query(Node).filter(Node.aliases.like(pattern))
    if node_type:
        q = q.filter(Node.node_type == node_type)
    return q.order_by(Node.created_at.desc()).all()


def _find_nodes_by_label_containment(
    session, label: str, node_type: Optional[str] = None, limit: int = 20
) -> List[Node]:
    """Find nodes where one label contains the other (bidirectional)."""
    if not label or len(label) < 3:
        return []
    label_lower = label.strip().lower()
    escaped = label_lower.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    q = session.query(Node).filter(
        func.lower(Node.label).like(f"%{escaped}%")
    )
    if node_type:
        q = q.filter(Node.node_type == node_type)
    results = q.order_by(Node.created_at.desc()).limit(limit).all()
    return [n for n in results if n.label and n.label.strip().lower() != label_lower]


def _find_nodes_where_label_matches_aliases(
    session, aliases: List[str], node_type: Optional[str] = None
) -> List[Node]:
    """Reverse alias check: find nodes whose label matches any of the given aliases."""
    if not aliases:
        return []
    from sqlalchemy import or_
    alias_conditions = [func.lower(Node.label) == a.strip().lower() for a in aliases if a and a.strip()]
    if not alias_conditions:
        return []
    q = session.query(Node).filter(or_(*alias_conditions))
    if node_type:
        q = q.filter(Node.node_type == node_type)
    return q.order_by(Node.created_at.desc()).all()


def _find_fuzzy_match_nodes(
    session, label: str, node_type: Optional[str] = None, threshold: float = 0.8, limit: int = 20
) -> List[Tuple[Node, float]]:
    query = session.query(Node)
    if node_type:
        query = query.filter(Node.node_type == node_type)
    all_nodes = query.order_by(Node.created_at.desc()).limit(3000).all()
    query_emb = create_embedding(label)
    matches: List[Tuple[Node, float]] = []
    for node in all_nodes:
        node_emb = create_embedding(node.label or "")
        sim = cosine_similarity(query_emb, node_emb)
        if sim >= threshold:
            matches.append((node, sim))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[:limit]


def _find_by_semantic_label(
    session, semantic_label: str, node_type: Optional[str] = None, threshold: float = 0.75
) -> List[Node]:
    q = session.query(Node).filter(Node.semantic_label.isnot(None))
    if node_type:
        q = q.filter(Node.node_type == node_type)
    all_nodes = q.order_by(Node.created_at.desc()).limit(3000).all()
    query_emb = create_embedding(semantic_label)
    out: List[Tuple[Node, float]] = []
    for n in all_nodes:
        emb = create_embedding(n.semantic_label or "")
        sim = cosine_similarity(query_emb, emb)
        if sim >= threshold:
            out.append((n, sim))
    out.sort(key=lambda x: x[1], reverse=True)
    return [n for n, _ in out]


def _rank_candidates(candidates: List[Node], label: str, semantic_label: Optional[str], category: Optional[str]) -> List[Node]:
    if not candidates:
        return []
    label_emb = create_embedding(label or "")
    semantic_emb = create_embedding(semantic_label or "") if semantic_label else None
    scored: List[Tuple[Node, float]] = []
    for cand in candidates:
        score = 0.0
        cand_label_emb = create_embedding(cand.label or "")
        score += cosine_similarity(label_emb, cand_label_emb) * 0.4
        if semantic_emb is not None and cand.semantic_label:
            cand_sem_emb = create_embedding(cand.semantic_label or "")
            score += cosine_similarity(semantic_emb, cand_sem_emb) * 0.3
        if category and cand.category:
            if category.lower() == cand.category.lower():
                score += 0.15
            elif category.lower() in cand.category.lower() or cand.category.lower() in category.lower():
                score += 0.075
        if cand.importance is not None:
            score += float(cand.importance) * 0.15
        else:
            score += 0.5 * 0.15
        scored.append((cand, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored]


def find_merge_candidates_semantic(
    session,
    *,
    label: str,
    node_type: Optional[str] = None,
    semantic_label: Optional[str] = None,
    category: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    k: int = 5,
) -> List[Node]:
    candidates: Dict[str, Node] = {}
    from app.assistant.kg_core.user_identity import get_primary_user_name, get_primary_user_full_name
    from app.assistant.utils.assistant_name import get_assistant_name
    _user = get_primary_user_name().lower()
    _user_full = get_primary_user_full_name().lower()
    _asst = get_assistant_name().lower()
    well_known_entities = {_user, _user_full, _asst, f"{_asst}_ai", f"{_asst} ai", f"{_asst} ai assistant", f"{_asst}_ai_assistant"}

    if (label or "").strip().lower() in well_known_entities:
        # Always search by the canonical preferred_name so full-name variants
        # resolve to the primary user node, not to other duplicate full-name nodes.
        label_lower = (label or "").strip().lower()
        if label_lower in (_user_full,):
            canonical_label = get_primary_user_name()
        else:
            canonical_label = label
        for node in _find_case_insensitive_exact_match_nodes(session, canonical_label, node_type):
            candidates[str(node.id)] = node
        ranked = _rank_candidates(list(candidates.values()), label, semantic_label, category)
        return ranked[:k]

    # Tier 0: exact label match
    for node in _find_case_insensitive_exact_match_nodes(session, label, node_type):
        candidates[str(node.id)] = node
    if len(candidates) < k * 2:
        for node in _find_exact_match_nodes(session, label, node_type):
            candidates[str(node.id)] = node

    # Tier 1a: incoming label appears in existing node's aliases
    if len(candidates) < k * 2:
        for node in _find_nodes_by_alias(session, label, node_type):
            candidates[str(node.id)] = node

    # Tier 1b: existing node's label matches one of incoming node's aliases
    if len(candidates) < k * 2 and aliases:
        for node in _find_nodes_where_label_matches_aliases(session, aliases, node_type):
            candidates[str(node.id)] = node

    # Tier 2: label containment (one label is substring of the other)
    if len(candidates) < k * 2:
        for node in _find_nodes_by_label_containment(session, label, node_type):
            candidates[str(node.id)] = node

    # Tier 3: embedding similarity
    if len(candidates) < k * 2:
        for node, _score in _find_fuzzy_match_nodes(session, label, node_type):
            candidates[str(node.id)] = node
    if semantic_label and len(candidates) < k * 2:
        for node in _find_by_semantic_label(session, semantic_label, node_type):
            candidates[str(node.id)] = node

    ranked = _rank_candidates(list(candidates.values()), label, semantic_label, category)
    return ranked[:k]


def build_node_candidate_payload(candidates: List[Node]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for c in candidates:
        rows.append(
            {
                "id": str(c.id),
                "node_type": c.node_type,
                "label": c.label,
                "description": c.description,
                "aliases": c.aliases or [],
                "hash_tags": c.hash_tags or [],
                "semantic_label": c.semantic_label,
                "goal_status": c.goal_status,
                "valid_during": c.valid_during,
                "category": c.category,
                "start_date": c.start_date.isoformat() if c.start_date and hasattr(c.start_date, "isoformat") else c.start_date,
                "end_date": c.end_date.isoformat() if c.end_date and hasattr(c.end_date, "isoformat") else c.end_date,
                "start_date_confidence": c.start_date_confidence,
                "end_date_confidence": c.end_date_confidence,
                "confidence": c.confidence,
                "importance": c.importance,
            }
        )
    return rows


def merge_node_fields_into_existing(existing: Node, new_data: Dict[str, Any]) -> None:
    existing_aliases = set(existing.aliases or [])
    existing_aliases.update(new_data.get("aliases") or [])
    existing.aliases = sorted(existing_aliases)

    existing_tags = set(existing.hash_tags or [])
    existing_tags.update(new_data.get("hash_tags") or [])
    existing.hash_tags = sorted(existing_tags)

    if not existing.semantic_label and new_data.get("semantic_label"):
        existing.semantic_label = new_data.get("semantic_label")
    if not existing.goal_status and new_data.get("goal_status"):
        existing.goal_status = new_data.get("goal_status")
    if not existing.valid_during and new_data.get("valid_during"):
        existing.valid_during = new_data.get("valid_during")
    if not existing.category and new_data.get("category"):
        existing.category = new_data.get("category")

    # Prefer keeping existing dates, only fill missing.
    if not existing.start_date:
        existing.start_date = normalize_iso_datetime(new_data.get("start_date"))
    if not existing.end_date:
        existing.end_date = normalize_iso_datetime(new_data.get("end_date"))
    if not existing.start_date_confidence and new_data.get("start_date_confidence"):
        existing.start_date_confidence = new_data.get("start_date_confidence")
    if not existing.end_date_confidence and new_data.get("end_date_confidence"):
        existing.end_date_confidence = new_data.get("end_date_confidence")

    if existing.confidence is None and new_data.get("confidence") is not None:
        existing.confidence = new_data.get("confidence")
    if existing.importance is None and new_data.get("importance") is not None:
        existing.importance = new_data.get("importance")


def normalize_confidence_value(conf_val):
    if conf_val is None:
        return None
    if isinstance(conf_val, (int, float)):
        return float(conf_val)
    if isinstance(conf_val, str):
        s = conf_val.strip().lower()
        if s in {"", "unknown", "null", "none", ":null", ":null,", "/null", "_null", "/none", "_none"}:
            return None
        if s.startswith(":null") or s.startswith("/null"):
            return None
        try:
            return float(s)
        except Exception as exc:
            logger.debug("normalize_confidence_value: could not parse %r: %s", conf_val, exc)
            return None
    return None


def apply_node_data_merger_result(existing: Node, merger_result: Dict[str, Any]) -> None:
    if merger_result.get("merged_aliases"):
        existing.aliases = merger_result.get("merged_aliases") or []
    if merger_result.get("merged_hash_tags"):
        existing.hash_tags = merger_result.get("merged_hash_tags") or []
    if merger_result.get("unified_semantic_label"):
        existing.semantic_label = merger_result.get("unified_semantic_label")
    if merger_result.get("unified_goal_status"):
        existing.goal_status = merger_result.get("unified_goal_status")
    if merger_result.get("unified_valid_during"):
        existing.valid_during = merger_result.get("unified_valid_during")
    if merger_result.get("unified_category"):
        existing.category = merger_result.get("unified_category")
    if "unified_start_date" in merger_result:
        existing.start_date = normalize_iso_datetime(merger_result.get("unified_start_date"))
    if "unified_end_date" in merger_result:
        existing.end_date = normalize_iso_datetime(merger_result.get("unified_end_date"))
    if "unified_start_date_confidence" in merger_result:
        existing.start_date_confidence = str(merger_result.get("unified_start_date_confidence")) if merger_result.get("unified_start_date_confidence") is not None else None
    if "unified_end_date_confidence" in merger_result:
        existing.end_date_confidence = str(merger_result.get("unified_end_date_confidence")) if merger_result.get("unified_end_date_confidence") is not None else None
    if "unified_confidence" in merger_result:
        existing.confidence = normalize_confidence_value(merger_result.get("unified_confidence"))
    if "unified_importance" in merger_result:
        existing.importance = normalize_confidence_value(merger_result.get("unified_importance"))


def create_node_from_standardized(session, node_data: Dict[str, Any]) -> Node:
    label = node_data.get("label") or ""
    n = Node(
        node_type=node_data.get("node_type") or "Entity",
        label=label,
        category=node_data.get("category"),
        aliases=node_data.get("aliases") or [],
        description="",
        attributes={},
        valid_during=node_data.get("valid_during"),
        hash_tags=node_data.get("hash_tags") or [],
        start_date=normalize_iso_datetime(node_data.get("start_date")),
        end_date=normalize_iso_datetime(node_data.get("end_date")),
        start_date_confidence=node_data.get("start_date_confidence"),
        end_date_confidence=node_data.get("end_date_confidence"),
        semantic_label=node_data.get("semantic_label"),
        goal_status=node_data.get("goal_status"),
        confidence=node_data.get("confidence"),
        importance=node_data.get("importance"),
        source=node_data.get("source"),
        original_message_id=node_data.get("original_message_id"),
        original_sentence=node_data.get("sentence"),
        sentence_id=node_data.get("sentence_id"),
    )
    session.add(n)
    session.flush()
    return n


def find_edge_candidates(session, source_id: str, target_id: str, relationship_type: str, limit: int = 5) -> List[Edge]:
    candidates = (
        session.query(Edge)
        .filter(
            Edge.source_id == source_id,
            Edge.target_id == target_id,
            Edge.relationship_type == relationship_type,
        )
        .limit(limit)
        .all()
    )
    if len(candidates) < limit:
        others = (
            session.query(Edge)
            .filter(
                Edge.source_id == source_id,
                Edge.target_id == target_id,
                Edge.relationship_type != relationship_type,
            )
            .limit(limit - len(candidates))
            .all()
        )
        candidates.extend(others)
    return candidates[:limit]


def build_edge_candidate_payload(candidates: List[Edge]) -> List[Dict[str, Any]]:
    rows = []
    for idx, c in enumerate(candidates):
        rows.append(
            {
                "candidate_id": idx + 1,
                "id": str(c.id),
                "relationship_type": c.relationship_type,
                "source_node_label": c.source_node.label if c.source_node else "Unknown",
                "target_node_label": c.target_node.label if c.target_node else "Unknown",
                "sentence": c.sentence or "",
                "context_window": "",
                "sentence_window": "",
            }
        )
    return rows


def create_edge_if_missing(
    session,
    *,
    source_id: str,
    target_id: str,
    relationship_type: str,
    sentence: Optional[str],
    source: Optional[str],
    original_message_id: Optional[str],
    sentence_id: Optional[str],
    relationship_descriptor: Optional[str] = None,
) -> Tuple[Edge, str]:
    existing = (
        session.query(Edge)
        .filter(
            Edge.source_id == source_id,
            Edge.target_id == target_id,
            Edge.relationship_type == relationship_type,
        )
        .first()
    )
    if existing:
        return existing, "found_exact"

    e = Edge(
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
        relationship_descriptor=relationship_descriptor,
        attributes={},
        sentence=sentence,
        confidence=None,
        importance=None,
        source=source,
        original_message_id=original_message_id,
        sentence_id=sentence_id,
    )
    session.add(e)
    session.flush()
    return e, "created"


# ---------------------------------------------------------------------------
# Provenance resolution
# ---------------------------------------------------------------------------

class WindowProvenance:
    """
    Resolved provenance for a single conversation window.

    Carries everything needed to write a KGNodeEvidence or KGEdgeEvidence row:
      - source_table / source_id   → stable pointer to the originating log row
      - source_text                → verbatim raw-chat fragment (parser context)
      - message_timestamp          → when the conversation happened
      - window_id                  → for broader context lookups
    """

    __slots__ = (
        "window_id",
        "source_table",
        "source_id",
        "source_text",
        "message_timestamp",
    )

    def __init__(
        self,
        *,
        window_id: str,
        source_table: Optional[str],
        source_id: Optional[str],
        source_text: Optional[str],
        message_timestamp,
    ) -> None:
        self.window_id = window_id
        self.source_table = source_table
        self.source_id = source_id
        self.source_text = source_text
        self.message_timestamp = message_timestamp


def resolve_window_provenance(session, window_id: str) -> WindowProvenance:
    """
    Walk the staging tables to resolve full provenance for a window.

    Chain:  window → window_items → kg_chat_projection → unified_log_id / source_table
            window → kg_chat_parsed_sentence (context texts)

    Returns a WindowProvenance with best-effort values; fields are None when
    the staging data is unavailable (e.g. for windows processed before this
    feature was added).
    """
    from app.assistant.database.kg_chat_projection import (
        KGChatConversationWindow,
        KGChatConversationWindowItem,
        KGChatParsedSentence,
        KGChatProjection,
    )

    # --- window timestamp ---
    window = session.query(KGChatConversationWindow).filter(
        KGChatConversationWindow.id == window_id
    ).first()
    message_timestamp = window.end_unified_timestamp if window else None

    # --- earliest projection row for this window → unified_log_id + source_table ---
    item = (
        session.query(KGChatConversationWindowItem)
        .filter(KGChatConversationWindowItem.window_id == window_id)
        .order_by(KGChatConversationWindowItem.item_order.asc())
        .first()
    )

    source_table: Optional[str] = None
    source_id: Optional[str] = None

    if item:
        projection = session.query(KGChatProjection).filter(
            KGChatProjection.id == item.projection_id
        ).first()
        if projection:
            source_id = projection.unified_log_id
            # Source table name is stored on the projection model itself so
            # future table renames are captured at ingestion time.
            source_table = KGChatProjection.__tablename__.replace(
                "kg_chat_projection", "unified_log_2026"
            )
            # More precisely: read it from the projection record if we ever
            # add a source_table column there; fall back to the known name.
            # For now derive it from the projection's own table name mapping.
            source_table = _resolve_source_table(projection)

    # --- collect parser context texts for this window ---
    parsed_sentences = (
        session.query(KGChatParsedSentence)
        .filter(KGChatParsedSentence.window_id == window_id)
        .order_by(KGChatParsedSentence.sentence_order.asc())
        .all()
    )
    context_fragments = [
        ps.context for ps in parsed_sentences if ps.context and ps.context.strip()
    ]
    source_text = " | ".join(context_fragments) if context_fragments else None

    return WindowProvenance(
        window_id=window_id,
        source_table=source_table,
        source_id=source_id,
        source_text=source_text,
        message_timestamp=message_timestamp,
    )


def _resolve_source_table(projection) -> str:
    """
    Determine the source log table name for a KGChatProjection row.

    The projection was created by scanning a specific unified_log table.
    ProjectChatFromUnifiedLogStep reads from UnifiedLog2026 which has
    __tablename__ = 'unified_log_2026'.  We store that name so future
    renames (unified_log_2027, etc.) are captured at ingestion rather than
    hard-coded here.
    """
    from app.assistant.database.db_handler import UnifiedLog2026
    return UnifiedLog2026.__tablename__


def write_node_evidence(
    session,
    *,
    node_id: str,
    provenance: WindowProvenance,
    derived_sentence: Optional[str],
    merge_action: str,
) -> None:
    """Append one evidence row for a node observation."""
    from app.assistant.database.kg_chat_projection import KGNodeEvidence

    session.add(
        KGNodeEvidence(
            id=str(uuid.uuid4()),
            node_id=node_id,
            source_table=provenance.source_table,
            source_id=provenance.source_id,
            source_text=provenance.source_text,
            derived_sentence=derived_sentence,
            message_timestamp=provenance.message_timestamp,
            window_id=provenance.window_id,
            merge_action=merge_action,
        )
    )


def write_edge_evidence(
    session,
    *,
    edge_id: str,
    provenance: WindowProvenance,
    derived_sentence: Optional[str],
    merge_action: str,
) -> None:
    """Append one evidence row for an edge observation."""
    from app.assistant.database.kg_chat_projection import KGEdgeEvidence

    session.add(
        KGEdgeEvidence(
            id=str(uuid.uuid4()),
            edge_id=edge_id,
            source_table=provenance.source_table,
            source_id=provenance.source_id,
            source_text=provenance.source_text,
            derived_sentence=derived_sentence,
            message_timestamp=provenance.message_timestamp,
            window_id=provenance.window_id,
            merge_action=merge_action,
        )
    )

