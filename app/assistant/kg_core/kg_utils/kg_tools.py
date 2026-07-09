"""
kg_tools.py - Utility functions for manipulating the knowledge graph nodes and edges.
"""

# NodeType is now an ENUM, no longer a separate table

import uuid
from uuid import UUID

from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.vector_utils import cosine_similarity

from collections import defaultdict, deque

logger = get_logger(__name__)

from typing import Any, Dict, List, Optional, Tuple, Union
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func


# Note: create_embedding and cosine_similarity moved to knowledge_graph_utils.py
# Use KnowledgeGraphUtils class for these functions


def _endpoint_label_and_type(node, endpoint_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve an edge endpoint's display label and type.

    For regular kg_nodes, reads ``.label`` / ``.node_type``. For pod URIs
    (``datapod:<kind>:<hash>``) — which are valid edge endpoints with no
    kg_node_metadata row — falls back to pod_store metadata so the response
    surfaces a usable label instead of None.
    """
    if node is not None:
        return node.label, node.node_type
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    if endpoint_id and POD_URI_RE.fullmatch(endpoint_id):
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(endpoint_id)
        if pod is not None:
            return (pod.one_liner or endpoint_id)[:200], "Pod"
    return None, None


def delete_node(node_id, session: Session):
    """
    Delete a node with its FULL dependent lifecycle: its edges (the DB-level
    ON DELETE CASCADE was dropped 2026-05-10 — nothing cascades), those
    edges' kg_edge_evidence rows, the node's kg_node_evidence rows, and
    supersede active duplicate-scan verdicts naming it. Section tags cascade
    via their surviving FK; Chroma vectors are removed by the ORM
    after_delete chokepoint post-commit.

    Does NOT commit — the caller owns the transaction boundary.
    """
    from sqlalchemy import text as _sql_text

    from app.assistant.kg_core.kg_utils.node_merge import (
        cleanup_edge_evidence,
        cleanup_node_dependents_on_delete,
    )

    node_id = str(node_id)
    node = session.query(Node).filter_by(id=node_id).first()
    if not node:
        raise ValueError("Node not found.")

    edge_ids = [
        str(r[0]) for r in session.execute(
            _sql_text(
                "SELECT id FROM kg_edge_metadata "
                "WHERE source_id = :nid OR target_id = :nid"
            ),
            {"nid": node_id},
        ).fetchall()
    ]
    cleanup_edge_evidence(session, edge_ids)
    if edge_ids:
        session.execute(
            _sql_text(
                "DELETE FROM kg_edge_metadata "
                "WHERE source_id = :nid OR target_id = :nid"
            ),
            {"nid": node_id},
        )
    cleanup_node_dependents_on_delete(session, node_id)
    # ORM delete (not raw SQL) so the embedding-sync chokepoint queues the
    # node's Chroma vector removal for after_commit. passive_deletes=True
    # keeps the ORM from touching the already-deleted edge rows.
    session.delete(node)





def describe_node(node_id, session: Session, filters: Dict[str, Any] = None, max_edges: int = 50) -> Dict[str, Any]:
    """
    Return a dict containing the node's core fields plus all incoming
    and outgoing edges (with their labels and types).
    
    Args:
        node_id: UUID or string of the node to describe
        session: Database session
        filters: Optional filters including temporal filters (start_date, end_date)
        max_edges: Maximum number of edges to return (default: 50). If more edges exist,
                   a random sample of max_edges will be returned with a warning.
    """
    from sqlalchemy import and_
    
    # Convert UUID to string for SQLite compatibility
    node_id = str(node_id)
    node = session.get(Node, node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")

    # Check if we have any filters
    start_date = filters.get("start_date") if filters else None
    end_date = filters.get("end_date") if filters else None
    node_types = filters.get("node_types") if filters else None
    relationship_types = filters.get("relationship_types") if filters else None
    text_filter = filters.get("text") if filters else None
    
    if start_date or end_date or node_types or relationship_types:
        # Use TemporalGraphFilter for efficient filtering
        temporal_filter = TemporalGraphFilter(session, start_date, end_date, node_types, relationship_types)
        # Always include the base node in valid nodes, regardless of filters
        valid_node_ids = temporal_filter._get_valid_node_ids(base_node_id=node_id)
        valid_edge_ids = temporal_filter._get_valid_edge_ids()
        
        # Fetch edges with filtering
        inbound: List[Edge] = (
            session.query(Edge)
                .filter(
                    and_(
                        Edge.target_id == node.id,
                        Edge.id.in_(valid_edge_ids)
                    )
                )
                .all()
        )
        outbound: List[Edge] = (
            session.query(Edge)
                .filter(
                    and_(
                        Edge.source_id == node.id,
                        Edge.id.in_(valid_edge_ids)
                    )
                )
                .all()
        )
    else:
        # Original behavior - fetch all edges
        inbound: List[Edge] = (
            session.query(Edge)
                .filter(Edge.target_id == node.id)
                .all()
        )
        outbound: List[Edge] = (
            session.query(Edge)
                .filter(Edge.source_id == node.id)
                .all()
        )

    # Apply text filtering if provided
    if text_filter:
        from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
        kg_utils = KnowledgeGraphUtils(session)
        
        # Create embedding for the text filter
        text_embedding = kg_utils.create_embedding(text_filter)
        k_matches = 10  # Top K matches to return
        
        # First, collect all edges with similarity scores
        all_edge_scores = []
        logger.debug(f"Text filtering: '{text_filter}' - collecting top {k_matches} matches")
        logger.debug(f"Checking {len(inbound)} inbound edges for text matches")
        
        for edge in inbound:
            if edge.sentence and edge.sentence_embedding is not None:
                similarity = cosine_similarity(text_embedding, edge.sentence_embedding)
                logger.debug(f"Edge sentence: '{edge.sentence[:50]}...' similarity: {similarity:.3f}")
                
                # Store edge with similarity score
                edge._similarity_score = similarity
                all_edge_scores.append((edge, similarity, 'inbound'))
        
        # Get top K matches from inbound edges
        all_edge_scores.sort(key=lambda x: x[1], reverse=True)  # Sort by similarity
        top_matches = all_edge_scores[:k_matches]
        inbound_filtered = [edge for edge, score, direction in top_matches if direction == 'inbound']
        
        logger.debug(f"Selected top {len(inbound_filtered)} inbound edges")
        
        # Collect outbound edges with similarity scores
        logger.debug(f"Checking {len(outbound)} outbound edges for text matches")
        for edge in outbound:
            if edge.sentence and edge.sentence_embedding is not None:
                similarity = cosine_similarity(text_embedding, edge.sentence_embedding)
                logger.debug(f"Edge sentence: '{edge.sentence[:50]}...' similarity: {similarity:.3f}")
                
                # Store edge with similarity score
                edge._similarity_score = similarity
                all_edge_scores.append((edge, similarity, 'outbound'))
        
        # Get top K matches from all edges (inbound + outbound)
        all_edge_scores.sort(key=lambda x: x[1], reverse=True)  # Sort by similarity
        top_matches = all_edge_scores[:k_matches]
        inbound_filtered = [edge for edge, score, direction in top_matches if direction == 'inbound']
        outbound_filtered = [edge for edge, score, direction in top_matches if direction == 'outbound']
        
        logger.debug(f"Selected top {len(inbound_filtered)} inbound + {len(outbound_filtered)} outbound edges")
        
        # Note: Node label filtering removed - using top-K edge filtering only
        
        # Update the edge lists with filtered results
        inbound = inbound_filtered
        outbound = outbound_filtered
        logger.debug(f"Text filtering results: {len(inbound)} inbound, {len(outbound)} outbound edges")

    # Check if we need to sample edges due to size limit
    total_edges = len(inbound) + len(outbound)
    warning_message = None
    
    if total_edges > max_edges:
        import random
        
        # Combine all edges and sample randomly
        all_edges = [(e, 'inbound') for e in inbound] + [(e, 'outbound') for e in outbound]
        sampled_edges = random.sample(all_edges, max_edges)
        
        # Separate back into inbound and outbound
        inbound = [e for e, direction in sampled_edges if direction == 'inbound']
        outbound = [e for e, direction in sampled_edges if direction == 'outbound']
        
        warning_message = (
            f"WARNING: Node has {total_edges} connections (exceeds limit of {max_edges}). "
            f"Showing {max_edges} randomly selected connections. "
            f"Consider filtering by: 1) specific node_id to focus on key nodes, 2) node_type=State for preferences/properties, 3) start_date/end_date for time frames, or 4) text for semantic filtering."
        )
        logger.warning(warning_message)
    
    # Currency annotation — a State/Event is "active" iff its era hasn't
    # ended as of now. Non-State/Event nodes report active=True (no decay).
    # Naive UTC matches Node.end_date column reads.
    _now_for_currency = datetime.now(timezone.utc).replace(tzinfo=None)
    _is_active = _state_or_event_is_active(node, _now_for_currency)

    # build the output
    details: Dict[str, Any] = {
        "id": str(node.id),
        "label": node.label,
        "semantic_label": node.semantic_label,
        "node_type": node.node_type,
        "category": node.category,
        "description": node.description,
        "aliases": node.aliases or [],
        "attributes": node.attributes or {},
        "valid_during": node.valid_during,
        "hash_tags": node.hash_tags or [],
        "goal_status": node.goal_status,
        "confidence": node.confidence,
        "importance": node.importance,
        "source": node.source,
        "start_date": node.start_date.isoformat() if node.start_date else None,
        "end_date": node.end_date.isoformat() if node.end_date else None,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        # Currency marker — False means era has elapsed; agents should
        # treat as historical, not current. See project_kg_redesign_v2
        # memo (era-bound decay).
        "is_active": _is_active,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
        "inbound_edges": [],
        "outbound_edges": []
    }
    
    # Add warning message if present
    if warning_message:
        details["warning"] = warning_message

    for e in inbound:
        # load the source node so we can show its label. For pod-URI endpoints
        # (no kg_node row by design), fall back to pod_store metadata so agents
        # see "[image: ...]" / "[chat_cluster: ...]" instead of a null label.
        src = session.get(Node, e.source_id)
        src_label, src_type = _endpoint_label_and_type(src, e.source_id)
        edge_data = {
            "edge_id": str(e.id),
            "from_node_id": str(e.source_id),
            "from_node_label": src_label,
            "from_node_type": src_type,
            "from_is_active": _state_or_event_is_active(src, _now_for_currency) if src else True,
            "edge_type": e.relationship_type,
            "sentence": e.sentence
        }
        if hasattr(e, '_similarity_score'):
            edge_data["similarity_score"] = e._similarity_score
        details["inbound_edges"].append(edge_data)

    for e in outbound:
        tgt = session.get(Node, e.target_id)
        tgt_label, tgt_type = _endpoint_label_and_type(tgt, e.target_id)
        edge_data = {
            "edge_id": str(e.id),
            "to_node_id": str(e.target_id),
            "to_node_label": tgt_label,
            "to_node_type": tgt_type,
            "to_is_active": _state_or_event_is_active(tgt, _now_for_currency) if tgt else True,
            "edge_type": e.relationship_type,
            "sentence": e.sentence
        }
        if hasattr(e, '_similarity_score'):
            edge_data["similarity_score"] = e._similarity_score
        details["outbound_edges"].append(edge_data)

    return details


def _state_or_event_is_active(node, now=None) -> bool:
    """Is this node's era still in force as of ``now``?

    - For State/Event nodes: True iff ``end_date`` is NULL or > now.
      Also False when ``end_date_confidence == "auto_decay"`` AND the
      end_date has elapsed (redundant but explicit).
    - For all other node types: True. Entities/Concepts/etc. are timeless
      identities, not decaying states.

    Used by describe_node to annotate currency on every serialized node
    so agent-facing tools can distinguish "currently true" from
    "historically observed" without running their own filter queries.
    """
    if node is None:
        return True
    node_type = getattr(node, "node_type", None)
    if node_type not in ("State", "Event"):
        return True
    end_date = getattr(node, "end_date", None)
    if end_date is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Normalize both sides to naive UTC. end_date may arrive aware when a
    # caller has just assigned `datetime.now(timezone.utc)` to a Node row
    # before the session refreshes (Node.end_date reads back naive after
    # round-trip, but the in-memory aware value lives until then).
    if end_date.tzinfo is not None:
        end_date = end_date.astimezone(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return end_date > now


def delete_edge(edge_id: uuid.UUID, session: Session) -> bool:
    """
    Delete an edge and its kg_edge_evidence rows (no FK — evidence never
    cascades). Does not commit the session.
    """
    from app.assistant.kg_core.kg_utils.node_merge import cleanup_edge_evidence

    edge = session.get(Edge, edge_id)
    if not edge:
        logger.warning(f"Edge {edge_id} not found")
        return False

    cleanup_edge_evidence(session, [str(edge_id)])
    session.delete(edge)
    return True


# Note: create_embedding moved to knowledge_graph_utils.py
# Use KnowledgeGraphUtils.create_embedding() instead


def safe_add_relationship_by_id(
        db_session: Session,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        relationship_type: str,
        attributes: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[Edge], str]:
    """
    Creates and adds a relationship to the session using node IDs if it doesn't
    already exist. Does not commit the transaction.

    Uniqueness is determined by the combination of source ID, target ID,
    relationship type, and start/end times extracted from the attributes.

    Returns a tuple containing the Edge object and a status string:
    "created", "found", or "error_missing_nodes".
    """
    attributes = attributes or {}

    # Dates parse via the module-level _parse_iso helper (defined below).
    start_date = _parse_iso(attributes.get("start_date") or attributes.get("start_date"))
    end_date = _parse_iso(attributes.get("end_date") or attributes.get("end_date"))

    # 2. Check for an existing edge using the full composite key.
    from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
    kg_utils = KnowledgeGraphUtils(db_session)
    existing_edge = kg_utils.find_exact_match_relationship(
        source_id, target_id, relationship_type, start_date, end_date
    )

    if existing_edge:
        # Edge already exists, return it
        return existing_edge, "found"

    # 3. Fetch nodes to create a descriptive text for the embedding.
    source_node = db_session.get(Node, source_id)
    target_node = db_session.get(Node, target_id)

    if not source_node or not target_node:
        missing = []
        if not source_node: missing.append(f"source (ID: {source_id})")
        if not target_node: missing.append(f"target (ID: {target_id})")
        logger.warning(f"Could not create edge because nodes were not found: {', '.join(missing)}")
        return None, "error_missing_nodes"

    # Create a semantically rich sentence for the embedding
    embedding_text = f"{source_node.label} {relationship_type} {target_node.label}"

    # 4. Create the new edge object.
    edge_id = str(uuid.uuid4())
    new_edge = Edge(
        id=edge_id,
        source_id=str(source_id),
        target_id=str(target_id),
        relationship_type=relationship_type,
        start_date=start_date,
        end_date=end_date,
        attributes=attributes,
        # Note: embeddings are now stored in ChromaDB, not as columns
    )

    # 5. Add the new edge to the session for a future commit.
    db_session.add(new_edge)
    
    # Store edge embedding in ChromaDB
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    chroma = get_chroma_manager()
    edge_embedding = kg_utils.create_embedding(embedding_text)
    chroma.store_edge_embedding(edge_id, embedding_text, edge_embedding)

    return new_edge, "created"


def short_describe_node(
        node_or_id: Union[Node, str, UUID],
        session: Session,
        *,
        k_edges: int = 5,
        alias_max: int = 2,
        trivial_types: Optional[set] = None,
        importance_default: float = 0.5,
        include_sentences: bool = False,
) -> Dict[str, Any]:
    """
    Compact summary for a node, using per-edge importance if present.
    Fallback order for importance: Edge.attributes['importance'] -> Edge.importance -> default.
    """
    if not isinstance(session, Session):
        raise TypeError("session must be an SQLAlchemy Session instance")

    # Resolve node
    node = node_or_id if isinstance(node_or_id, Node) else session.get(Node, node_or_id)
    if not node:
        raise ValueError(f"Node not found: {node_or_id}")

    # Pull a bit extra, newest first
    inbound = (
        session.query(Edge)
            .filter(Edge.target_id == node.id)
            .order_by(desc(Edge.updated_at), desc(Edge.created_at))
            .limit(max(k_edges * 3, 10))
            .all()
    )
    outbound = (
        session.query(Edge)
            .filter(Edge.source_id == node.id)
            .order_by(desc(Edge.updated_at), desc(Edge.created_at))
            .limit(max(k_edges * 3, 10))
            .all()
    )
    edges = [("in", e) for e in inbound] + [("out", e) for e in outbound]

    def _edge_ts(e: Edge) -> Optional[datetime]:
        return e.updated_at or e.created_at

    scored: List[Dict[str, Any]] = []
    for direction, e in edges:
        other = e.source_node if direction == "in" else e.target_node
        if not other:
            continue

        # Importance priority: Edge.attributes > Edge.importance > default
        edge_imp = None
        if e.attributes:
            edge_imp = e.attributes.get("importance")
            if isinstance(edge_imp, str):
                try:
                    edge_imp = float(edge_imp)
                except ValueError:
                    edge_imp = None
        if edge_imp is None and e.importance is not None:
            edge_imp = e.importance
        imp = float(edge_imp) if edge_imp is not None else importance_default

        rec = recency_score(_edge_ts(e))
        score = 0.6 * imp + 0.4 * rec

        edge_info = {
            "edge_id": str(e.id),
            "direction": direction,
            "edge_type": e.relationship_type,
            "other_node_id": str(other.id),
            "other_node_label": other.label,
            "updated_at": (_edge_ts(e).isoformat() if _edge_ts(e) else None),
            "score": round(score, 4),
            "importance": round(float(imp), 4),
        }
        if include_sentences and e.sentence:
            edge_info["sentence"] = e.sentence
        scored.append(edge_info)

    scored.sort(key=lambda x: (x["score"], x["updated_at"] or ""), reverse=True)

    # Diversity caps
    trivial_types = trivial_types or {"has_email"}
    seen_per_type: Dict[str, int] = {}
    top_edges: List[Dict[str, Any]] = []
    for s in scored:
        et = s["edge_type"]
        cap = 1 if et in trivial_types else 2
        if seen_per_type.get(et, 0) >= cap:
            continue
        top_edges.append(s)
        seen_per_type[et] = seen_per_type.get(et, 0) + 1
        if len(top_edges) >= k_edges:
            break

    aliases = node.aliases or []
    aliases_preview = aliases[:alias_max]
    aliases_more_count = max(0, len(aliases) - len(aliases_preview))

    return {
        "node_id": str(node.id),
        "label": node.label,
        "node_type": node.node_type,
        "aliases_preview": aliases_preview,
        "aliases_more_count": aliases_more_count,
        "top_edges": top_edges,
        "start_date": node.start_date.isoformat() if node.start_date else None,
        "end_date": node.end_date.isoformat() if node.end_date else None,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
    }

# kg_tools.py
from typing import Any, Dict, Optional, Union
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        logger.debug("Could not parse ISO timestamp: %s", ts, exc_info=True)
        return None

def recency_score(ts: Optional[datetime], *, half_life_days: float = 90.0) -> float:
    """Exponential-decay recency weight in [0, 1] (1.0 = now, 0.5 at one half-life)."""
    if not ts:
        return 0.0
    now = datetime.now(timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)

def _is_active(start: Optional[datetime], end: Optional[datetime]) -> bool:
    now = datetime.now(timezone.utc)
    if start and now < start:
        return False
    if end and now >= end:
        return False
    return True

def describe_edge(
        edge_id: Union[str, UUID],
        session: Session,
        include_raw: bool = False
) -> Dict[str, Any]:
    """
    Return a concise description of an edge, reading credibility, importance,
    and qualifiers from Edge.attributes or Edge.importance column.
    """
    edge: Optional[Edge] = session.query(Edge).filter(Edge.id == edge_id).one_or_none()
    if not edge:
        raise ValueError(f"Edge not found: {edge_id}")

    src: Optional[Node] = session.query(Node).filter(Node.id == edge.source_id).one_or_none()
    tgt: Optional[Node] = session.query(Node).filter(Node.id == edge.target_id).one_or_none()

    attrs: Dict[str, Any] = dict(edge.attributes or {})

    # Pull primary scoring fields from attributes, then Edge columns
    credibility = attrs.get("credibility")
    importance = attrs.get("importance")
    
    # Fallback to Edge.importance column if not in attributes
    if importance is None and edge.importance is not None:
        importance = edge.importance

    # Final defaults
    credibility = float(credibility) if isinstance(credibility, (int, float, str)) and str(credibility).replace(".","",1).isdigit() else None
    importance  = float(importance)  if isinstance(importance, (int, float, str))  and str(importance).replace(".","",1).isdigit()  else 0.5

    decay_rate = attrs.get("decay_rate")
    try:
        decay_rate = float(decay_rate) if decay_rate is not None else None
    except Exception:
        logger.debug("Could not parse decay_rate value: %s", decay_rate, exc_info=True)
        decay_rate = None

    # Temporal fields, prefer attributes if present, else DB columns
    start_s = attrs.get("start_date") or attrs.get("start_date")
    end_s   = attrs.get("end_date") or attrs.get("end_date")
    start_dt = _parse_iso(start_s) or edge.start_date
    end_dt   = _parse_iso(end_s) or edge.end_date

    # Qualifiers
    qualifiers = {
        "details": attrs.get("details"),
        "location": attrs.get("location"),
        "time_of_day": attrs.get("time_of_day"),
        "emotion": attrs.get("emotion"),
        "mood": attrs.get("mood"),
        "frequency": attrs.get("frequency"),
        "tags": attrs.get("tags"),
        "suggested_qualifiers": attrs.get("suggested_qualifiers"),
    }

    # Provenance-like fields if you store them in attributes. The edge's
    # canonical per-observation timestamp now lives in kg_edge_evidence
    # (JOIN through edge_id) — this dict is just legacy-attribute scrape.
    provenance = {
        "reference_text": attrs.get("reference_text"),
        "provenance_timestamp": attrs.get("provenance_timestamp"),
        "source": attrs.get("data_source"),
    }

    # Headline synthesis, small and deterministic
    source_label = src.label if src else None
    target_label = tgt.label if tgt else None
    title = attrs.get("title") or attrs.get("role")
    if edge.relationship_type == "works_for" and source_label and target_label:
        headline = f"{source_label} works for {target_label}"
    elif edge.relationship_type == "works_as" and source_label and title:
        headline = f"{source_label} works as {title}"
    else:
        headline = f"{source_label} {edge.relationship_type} {target_label}".strip()

    data: Dict[str, Any] = {
        "edge_id": str(edge.id),
        "edge_type": edge.relationship_type,
        "source": {"id": str(edge.source_id), "label": source_label},
        "target": {"id": str(edge.target_id), "label": target_label},
        "created_at": edge.created_at.isoformat() if edge.created_at else None,
        "updated_at": edge.updated_at.isoformat() if edge.updated_at else None,
        "start_date": start_dt.isoformat() if start_dt else None,
        "end_date": end_dt.isoformat() if end_dt else None,
        "is_active": _is_active(start_dt, end_dt),
        "importance": importance,
        "credibility": credibility,
        "decay_rate": decay_rate,
        "valid_during": attrs.get("valid_during"),
        "qualifiers": qualifiers,
        "provenance": provenance,
        "attributes_preview": {k: v for k, v in attrs.items() if k in ("title", "role", "department", "email", "phone")},
        "headline": headline,
        "why": [
            f"importance {importance:.2f}" if importance is not None else None,
            "active" if _is_active(start_dt, end_dt) else "inactive",
            "recent" if edge.updated_at and (datetime.now(timezone.utc) - edge.updated_at).days < 180 else None,
        ],
    }
    data["why"] = [w for w in data["why"] if w]

    if include_raw:
        data["raw_attributes"] = attrs

    return data


def parse_filters_from_pydantic(filters) -> Dict[str, Any]:
    """
    Parse SearchFilters (Pydantic model or dict) into dictionary for apply_search_filters.
    
    Args:
        filters: SearchFilters Pydantic model, dict, or None
        
    Returns:
        Dictionary with filter values in appropriate types
    """
    if not filters:
        return {}
    
    parsed_filters = {}
    
    # Handle both Pydantic models and plain dictionaries
    if hasattr(filters, 'dict'):
        # Pydantic model - convert to dict, excluding None values
        filter_dict = filters.dict(exclude_none=True)
    elif isinstance(filters, dict):
        # Plain dictionary - filter out None values
        filter_dict = {k: v for k, v in filters.items() if v is not None}
    else:
        # Unknown type, return empty dict
        return {}
    
    for key, value in filter_dict.items():
        if value is not None:
            parsed_filters[key] = value
    
    # Convert singular node_id to plural node_ids for apply_search_filters
    if "node_id" in parsed_filters and parsed_filters["node_id"]:
        parsed_filters["node_ids"] = [parsed_filters["node_id"]]
        del parsed_filters["node_id"]
    
    return parsed_filters


def apply_search_filters(session: Session, base_query, filters: Dict[str, Any] = None) -> Any:
    """
    Applies search filters to any base SQLAlchemy query.
    Returns the filtered query that can be further refined.
    
    Philosophy: "No filters = everything eligible" - filters are restrictive, not prescriptive.
    
    Args:
        session: SQLAlchemy session
        base_query: Base SQLAlchemy query object
        filters: Dictionary of filter parameters
        
    Supported filters:
        - node_ids: List of node UUIDs to restrict search to
        - node_types: List of node types to filter by (Entity, Goal, State, etc.)
        - exclude_nodes: List of node UUIDs to exclude
        - start_date: ISO date string - only nodes valid after this date (temporal connectivity)
        - end_date: ISO date string - only nodes valid before this date (temporal connectivity)
        - max_hops: Integer - expand node_ids to their neighborhoods (requires node_ids, default=all connected)
        - relationship_types: List of relationship types for connected nodes
        
    Returns:
        Filtered SQLAlchemy query object
    """
    if not filters:
        return base_query
    
    from sqlalchemy import or_, and_, func
    from datetime import datetime
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge
    
    # Node ID restrictions with smart defaults
    if "node_ids" in filters and filters["node_ids"]:
        node_ids = filters["node_ids"]
        
        # Smart default: if max_hops not provided, look at ALL connected nodes (no hop limit)
        max_hops = filters.get("max_hops")
        
        if max_hops is not None and max_hops >= 0:
            # Get neighborhood nodes up to max_hops
            neighborhood_nodes = set()
            for node_id in node_ids:
                try:
                    if isinstance(node_id, str):
                        node_id = uuid.UUID(node_id)
                    neighborhood = get_neighborhood(session, node_id, depth=max_hops)
                    neighborhood_nodes.update([node.id for node in neighborhood["nodes"]])
                except (ValueError, TypeError):
                    # Invalid UUID, skip
                    continue
            
            if neighborhood_nodes:
                base_query = base_query.filter(Node.id.in_(list(neighborhood_nodes)))
        else:
            # No max_hops provided - get ALL connected nodes (no hop limit)
            # This is more complex - we need to get all nodes connected to the specified nodes
            connected_nodes = set()
            for node_id in node_ids:
                try:
                    if isinstance(node_id, str):
                        node_id = uuid.UUID(node_id)
                    # Get all connected nodes (no depth limit)
                    connected = get_connected_nodes(node_id, session, direction="any")
                    connected_nodes.update([node.id for node in connected])
                    # Also include the original node
                    connected_nodes.add(node_id)
                except (ValueError, TypeError):
                    # Invalid UUID, skip
                    continue
            
            if connected_nodes:
                base_query = base_query.filter(Node.id.in_(list(connected_nodes)))
    elif "max_hops" in filters and filters["max_hops"] > 0:
        # max_hops provided but no node_ids - ignore max_hops (can't expand from nothing)
        pass
    
    # Node type restrictions (no default - if not provided, include all types)
    if "node_types" in filters and filters["node_types"]:
        # Convert node types to proper case (database enum expects capitalized)
        valid_node_types = ['Entity', 'Event', 'State', 'Goal', 'Concept', 'Property']
        normalized_types = []
        for node_type in filters["node_types"]:
            if isinstance(node_type, str):
                # Try to match case-insensitively
                for valid_type in valid_node_types:
                    if node_type.lower() == valid_type.lower():
                        normalized_types.append(valid_type)
                        break
                else:
                    # If no match found, use the original (will cause error but that's expected)
                    normalized_types.append(node_type)
            else:
                normalized_types.append(node_type)
        
        if normalized_types:
            base_query = base_query.filter(Node.node_type.in_(normalized_types))
    
    # Taxonomy path filtering (no default - if not provided, include all taxonomies)
    if "taxonomy_paths" in filters and filters["taxonomy_paths"]:
        from app.assistant.kg_core.taxonomy.utils import get_taxonomy_by_path
        
        # Check if taxonomy tables exist before attempting the JOIN
        try:
            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(session.get_bind())
            if "node_taxonomy_links" not in inspector.get_table_names():
                logger.debug("apply_search_filters: taxonomy tables not found, skipping taxonomy_paths filter")
                filters = {k: v for k, v in filters.items() if k != "taxonomy_paths"}
        except Exception as e:
            logger.debug(f"apply_search_filters: could not check taxonomy tables, skipping filter: {e}")
            filters = {k: v for k, v in filters.items() if k != "taxonomy_paths"}

    if "taxonomy_paths" in filters and filters["taxonomy_paths"]:
        from app.assistant.kg_core.taxonomy.utils import get_taxonomy_by_path
        
        # Get taxonomy IDs for all specified paths
        taxonomy_ids = []
        for path in filters["taxonomy_paths"]:
            if isinstance(path, str):
                try:
                    # Get taxonomy by path (e.g., "entity > person")
                    taxonomy = get_taxonomy_by_path(session, path)
                    if taxonomy:
                        taxonomy_ids.append(taxonomy.id)
                except Exception as e:
                    # Invalid path, skip
                    continue
        
        if taxonomy_ids:
            # Filter nodes that are classified under any of the specified taxonomy paths
            from app.assistant.kg_core.taxonomy.models import NodeTaxonomyLink
            base_query = base_query.join(NodeTaxonomyLink, Node.id == NodeTaxonomyLink.node_id).filter(
                NodeTaxonomyLink.taxonomy_id.in_(taxonomy_ids)
            ).distinct()
    
    # Exclude specific nodes (no default - if not provided, exclude nothing)
    if "exclude_nodes" in filters and filters["exclude_nodes"]:
        try:
            exclude_uuids = []
            for node_id in filters["exclude_nodes"]:
                if isinstance(node_id, str):
                    exclude_uuids.append(uuid.UUID(node_id))
                else:
                    exclude_uuids.append(node_id)
            base_query = base_query.filter(~Node.id.in_(exclude_uuids))
        except (ValueError, TypeError):
            # Invalid UUIDs, skip this filter
            pass
    
    # Temporal filtering with temporal connectivity logic
    # Nodes without temporal bounds are always included
    # Nodes with temporal bounds are included only if they overlap with the filter range
    temporal_conditions = []
    
    if "start_date" in filters and filters["start_date"]:
        try:
            if isinstance(filters["start_date"], str):
                start_date = datetime.fromisoformat(filters["start_date"].replace("Z", "+00:00"))
            else:
                start_date = filters["start_date"]
            
            # Include nodes that:
            # 1. Have no end_date (ongoing), OR
            # 2. Have end_date >= start_date (still valid at start_date)
            temporal_conditions.append(
                or_(
                    Node.end_date.is_(None),  # No end date = always valid
                    Node.end_date >= start_date
                )
            )
        except (ValueError, TypeError):
            # Invalid date format, skip this filter
            pass
    
    if "end_date" in filters and filters["end_date"]:
        try:
            if isinstance(filters["end_date"], str):
                end_date = datetime.fromisoformat(filters["end_date"].replace("Z", "+00:00"))
            else:
                end_date = filters["end_date"]
            
            # Include nodes that:
            # 1. Have no start_date (always existed), OR  
            # 2. Have start_date <= end_date (existed at end_date)
            temporal_conditions.append(
                or_(
                    Node.start_date.is_(None),  # No start date = always existed
                    Node.start_date <= end_date
                )
            )
        except (ValueError, TypeError):
            # Invalid date format, skip this filter
            pass
    
    # Apply temporal conditions (all must be true)
    if temporal_conditions:
        base_query = base_query.filter(and_(*temporal_conditions))
    
    # Relationship type filtering (requires joining with edges)
    # No default - if not provided, include all relationship types
    if "relationship_types" in filters and filters["relationship_types"]:
        # Join with edges to filter by relationship types
        base_query = base_query.join(Edge, or_(
            Edge.source_id == Node.id,
            Edge.target_id == Node.id
        )).filter(Edge.relationship_type.in_(filters["relationship_types"]))
    
    # Importance filtering (no default - if not provided, include all importance levels)
    if "min_importance" in filters and filters["min_importance"] is not None:
        try:
            min_importance = float(filters["min_importance"])
            # This assumes importance is stored in node attributes
            # You might need to adjust this based on your actual schema
            base_query = base_query.filter(
                func.json_extract(Node.attributes, '$.importance') >= min_importance
            )
        except (ValueError, TypeError):
            # Invalid importance value, skip this filter
            pass
    
    return base_query


def semantic_find_node_by_text(text: str, session: Session, threshold: float = 0.8, k: int = 5, filters = None) -> List[
    Tuple[Node, float]]:
    """
    Perform semantic search against all node labels using embeddings.
    Now supports filtering to restrict search scope using the efficient TemporalGraphFilter approach.
    
    Args:
        text: Text to search for
        session: Database session
        threshold: Similarity threshold (0.0-1.0)
        k: Maximum number of results
        filters: SearchFilters Pydantic model or None
    """
    from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
    kg_utils = KnowledgeGraphUtils(session)
    

    # If filters are provided, use TemporalGraphFilter for efficient temporal filtering
    if filters:
        # Parse Pydantic SearchFilters model
        parsed_filters = parse_filters_from_pydantic(filters)
        
        # Check if we have temporal filters
        start_date = parsed_filters.get("start_date")
        end_date = parsed_filters.get("end_date")
        
        if start_date or end_date:
            # Use TemporalGraphFilter for efficient temporal filtering
            node_types = parsed_filters.get("node_types")
            relationship_types = parsed_filters.get("relationship_types")
            temporal_filter = TemporalGraphFilter(session, start_date, end_date, node_types, relationship_types)
            valid_node_ids = temporal_filter._get_valid_node_ids()
            
            if not valid_node_ids:
                return []  # No nodes match the temporal filters
            
            # Get filtered nodes
            filtered_nodes = session.query(Node).filter(Node.id.in_(valid_node_ids)).all()
        else:
            # Use the old approach for non-temporal filters
            base_query = session.query(Node)
            filtered_query = apply_search_filters(session, base_query, parsed_filters)
            filtered_nodes = filtered_query.all()
        
        if not filtered_nodes:
            return []  # No nodes match the filters
        
        # If text is empty, skip semantic matching and just return filtered nodes
        if not text or text.strip() == "":
            # Return filtered nodes sorted by importance (descending), then by created_at (descending for recency)
            sorted_nodes = sorted(
                filtered_nodes,
                key=lambda n: (
                    -(n.importance if n.importance is not None else 0.5),  # Higher importance first
                    -(n.created_at.timestamp() if n.created_at else 0)  # More recent first
                ),
            )
            # Return with score of 1.0 to indicate "exact match" (no semantic filtering applied)
            candidates = [(node, 1.0) for node in sorted_nodes[:k]]
        else:
            # Do semantic search manually on filtered nodes
            new_embedding = kg_utils.create_embedding(text)
            similarities = []
            for node in filtered_nodes:
                if node.label_embedding is not None:
                    sim = cosine_similarity(new_embedding, node.label_embedding)
                    if sim >= threshold:
                        similarities.append((node, sim))
            
            # Sort by similarity and return top k
            similarities.sort(key=lambda x: x[1], reverse=True)
            candidates = similarities[:k]
    else:
        # Original behavior - search all nodes
        candidates = kg_utils.find_fuzzy_match_node(label=text, similarity_threshold=threshold, max_results=k)
    
    return candidates  # [(Node, similarity_score), ...]













def get_connected_nodes(node_id: uuid.UUID, session: Session, direction: str = "any") -> List[Node]:
    """
    Return all nodes connected to the given node, in the specified direction.
    direction = 'in', 'out', or 'any' (default).
    """
    connected_node_ids = set()

    if direction in ("out", "any"):
        outgoing = session.query(Edge.target_id).filter(Edge.source_id == node_id).all()
        connected_node_ids.update([r[0] for r in outgoing])

    if direction in ("in", "any"):
        incoming = session.query(Edge.source_id).filter(Edge.target_id == node_id).all()
        connected_node_ids.update([r[0] for r in incoming])

    connected_node_ids.discard(node_id)  # just in case

    return session.query(Node).filter(Node.id.in_(connected_node_ids)).all()


def get_connected_nodes_with_edges(node_id: uuid.UUID, session: Session, direction: str = "any") -> List[
    Tuple[Node, Edge]]:
    """
    Return all (Node, Edge) pairs connected to the given node.
    direction = 'in', 'out', or 'any'
    """
    pairs = []

    if direction in ("out", "any"):
        outgoing = session.query(Edge).filter(Edge.source_id == node_id).all()
        for edge in outgoing:
            target = session.get(Node, edge.target_id)
            if target:
                pairs.append((target, edge))

    if direction in ("in", "any"):
        incoming = session.query(Edge).filter(Edge.target_id == node_id).all()
        for edge in incoming:
            source = session.get(Node, edge.source_id)
            if source:
                pairs.append((source, edge))

    return pairs




def create_node(session: Session, label: str, node_type_value: str, **kwargs) -> Node:
    """
    Create a new node in the knowledge graph.
    `kwargs` can include description, aliases, attributes, start_date, end_date, 
    start_date_confidence, end_date_confidence, etc.
    NOTE: This function does NOT commit the session. The caller is responsible for committing.
    """
    # Check if the node type is valid
    # Valid node types: Entity, Event, State, Goal, Concept, Property
    
    new_node = Node(
        label=label,
        node_type=node_type_value,
        description=kwargs.get("description"),
        aliases=kwargs.get("aliases", []),
        attributes=kwargs.get("attributes", {}),
        start_date=kwargs.get("start_date"),
        end_date=kwargs.get("end_date"),
        start_date_confidence=kwargs.get("start_date_confidence"),
        end_date_confidence=kwargs.get("end_date_confidence"),
        confidence=kwargs.get("confidence"),
        importance=kwargs.get("importance"),
        source=kwargs.get("source")
    )
    session.add(new_node)
    return new_node


def create_edge(session: Session, source_id: uuid.UUID, target_id: uuid.UUID, relationship_type_value: str, **kwargs) -> Edge:
    """
    Create a new edge between two nodes.
    `kwargs` can include attributes, start_date, end_date, etc.
    NOTE: This function does NOT commit the session. The caller is responsible for committing.
    Edge type validation is handled by the edge standardization system.
    """
    # Check if nodes exist
    source_node = session.get(Node, source_id)
    target_node = session.get(Node, target_id)
    if not source_node or not target_node:
        raise ValueError("Source or target node not found.")

    new_edge = Edge(
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type_value,
        attributes=kwargs.get("attributes", {}),
        start_date=kwargs.get("start_date"),
        end_date=kwargs.get("end_date"),
        confidence=kwargs.get("confidence"),
        importance=kwargs.get("importance"),
        source=kwargs.get("source")
    )
    session.add(new_edge)

    return new_edge


# You will also need a delete_edges function in kg_tools.py that does NOT commit:








def get_neighborhood(session: Session, node_id: uuid.UUID, depth: int = 1) -> Dict[str, Any]:
    """
    Retrieves the subgraph surrounding a node up to a certain depth.
    Returns a dictionary of all nodes and edges within the neighborhood.
    """
    # Convert UUID to string for SQLite compatibility
    node_id_str = str(node_id)
    
    neighborhood = {"nodes": set(), "edges": set()}
    queue = deque([(node_id_str, 0)])  # (current_node_id, current_depth)
    visited_nodes = {node_id_str}

    start_node = session.get(Node, node_id_str)
    if not start_node:
        return neighborhood

    neighborhood["nodes"].add(start_node)

    while queue:
        current_id, current_depth = queue.popleft()

        if current_depth >= depth:
            continue

        connected = get_connected_nodes_with_edges(current_id, session, direction="any")
        for node, edge in connected:
            neighborhood["edges"].add(edge)
            if node.id not in visited_nodes:
                visited_nodes.add(node.id)
                neighborhood["nodes"].add(node)
                queue.append((node.id, current_depth + 1))

    # Convert sets to lists for JSON serialization if needed
    neighborhood["nodes"] = list(neighborhood["nodes"])
    neighborhood["edges"] = list(neighborhood["edges"])
    return neighborhood


















def inspect_node_neighborhood(
        node_id: uuid.UUID,
        session: Session
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Returns:
        node_info: dict containing info about the main node.
        edge_list: flat list of enriched edges with connected node data, suitable for chunking.
    """
    # Convert UUID to string for SQLite compatibility
    node_id = str(node_id)
    node = session.get(Node, node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")

    node_info = {
        "id": str(node.id),
        "label": node.label,
        "semantic_label": node.semantic_label,
        "type": node.node_type,
        "description": node.description,
        "aliases": node.aliases or [],
        "attributes": node.attributes or {},
        "start_date": node.start_date.isoformat() if node.start_date else None,
        "end_date": node.end_date.isoformat() if node.end_date else None,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence
    }

    edges = []

    incoming = session.query(Edge).filter(Edge.target_id == node_id).all()
    outgoing = session.query(Edge).filter(Edge.source_id == node_id).all()

    for edge in incoming:
        src = session.get(Node, edge.source_id)
        if not src:
            continue
        # Per-observation provenance lives in kg_edge_evidence (JOIN through
        # edge_id). The denormalized columns (original_message_timestamp,
        # relationship_descriptor) were removed 2026-05-10 — readers wanting
        # timestamps should JOIN kg_edge_evidence.message_timestamp.
        edges.append({
            "direction": "in",
            "edge_type": edge.relationship_type,
            "edge_attributes": edge.attributes or {},
            "sentence": edge.sentence,
            "connected_node": {
                "id": str(src.id),
                "label": src.label,
                "type": src.node_type,
                "description": src.description,
                "aliases": src.aliases or []
            }
        })

    for edge in outgoing:
        tgt = session.get(Node, edge.target_id)
        if not tgt:
            continue
        edges.append({
            "direction": "out",
            "edge_type": edge.relationship_type,
            "edge_attributes": edge.attributes or {},
            "sentence": edge.sentence,
            "connected_node": {
                "id": str(tgt.id),
                "label": tgt.label,
                "type": tgt.node_type,
                "description": tgt.description,
                "aliases": tgt.aliases or []
            }
        })

    return node_info, edges


class TemporalGraphFilter:
    """
    A class that provides database-level temporal filtering for knowledge graph operations.
    Instead of loading data into memory, this creates database views/queries that can be
    reused across multiple operations.
    """
    
    def __init__(self, session: Session, start_date: Optional[str] = None, end_date: Optional[str] = None, 
                 node_types: Optional[List[str]] = None, relationship_types: Optional[List[str]] = None):
        self.session = session
        self.start_date = start_date
        self.end_date = end_date
        self.node_types = node_types
        self.relationship_types = relationship_types
        self._valid_node_ids = None
        self._valid_edge_ids = None
    
    def _parse_time(self, time_str: str) -> datetime:
        """Parse time string into datetime object."""
        try:
            if "T" in time_str:
                return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            else:
                return datetime.fromisoformat(time_str + "T00:00:00+00:00")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid time format: {time_str}")
    
    def _get_valid_node_ids(self, base_node_id: Optional[uuid.UUID] = None) -> set:
        """Get set of node IDs that are temporally valid and match node type filters.
        
        Args:
            base_node_id: If provided, this node will always be included regardless of filters
        """
        if self._valid_node_ids is not None:
            return self._valid_node_ids
        
        query = self.session.query(Node.id)
        
        # Apply temporal filtering
        if self.start_date:
            start_dt = self._parse_time(self.start_date)
            query = query.filter(
                or_(
                    Node.end_date.is_(None),
                    Node.end_date >= start_dt
                )
            )
        
        if self.end_date:
            end_dt = self._parse_time(self.end_date)
            query = query.filter(
                or_(
                    Node.start_date.is_(None),
                    Node.start_date <= end_dt
                )
            )
        
        # Apply node type filtering
        if self.node_types:
            query = query.filter(Node.node_type.in_(self.node_types))
        
        valid_node_ids = {node_id for node_id, in query.all()}
        
        # Always include the base node if specified, regardless of filters
        if base_node_id:
            valid_node_ids.add(base_node_id)
        
        self._valid_node_ids = valid_node_ids
        return self._valid_node_ids
    
    def _get_valid_edge_ids(self) -> set:
        """Get set of edge IDs that are temporally valid and match relationship type filters."""
        if self._valid_edge_ids is not None:
            return self._valid_edge_ids
        
        valid_node_ids = self._get_valid_node_ids()
        if not valid_node_ids:
            self._valid_edge_ids = set()
            return self._valid_edge_ids
        
        query = self.session.query(Edge.id).filter(
            and_(
                Edge.source_id.in_(valid_node_ids),
                Edge.target_id.in_(valid_node_ids)
            )
        )
        
        # Apply relationship type filtering
        if self.relationship_types:
            query = query.filter(Edge.relationship_type.in_(self.relationship_types))
        
        self._valid_edge_ids = {edge_id for edge_id, in query.all()}
        return self._valid_edge_ids
    
    def find_node_by_text(self, text: str, similarity_threshold: float = 0.7) -> Optional[Node]:
        """Find a node by text within the temporally-filtered graph."""
        valid_node_ids = self._get_valid_node_ids()
        if not valid_node_ids:
            return None
        
        # Use the existing semantic search but filter results by temporal validity
        results = semantic_find_node_by_text(
            text=text,
            session=self.session,
            threshold=similarity_threshold,
            k=10
        )
        
        # Filter results to only include temporally valid nodes
        for node, score in results:
            if node.id in valid_node_ids:
                return node
        
        return None
    
    def describe_node(self, node_id: uuid.UUID, max_hops: int = None) -> Dict[str, Any]:
        """Describe a node and its neighborhood within the temporally-filtered graph."""
        # Always include the base node in valid nodes, regardless of filters
        valid_node_ids = self._get_valid_node_ids(base_node_id=node_id)
        valid_edge_ids = self._get_valid_edge_ids()
        
        # Get the node
        node = self.session.query(Node).filter(Node.id == node_id).first()
        if not node:
            return {"error": "Node not found"}
        
        # Get connected edges (only temporally valid ones)
        edge_query = self.session.query(Edge).filter(
            and_(
                Edge.id.in_(valid_edge_ids),
                or_(
                    Edge.source_id == node_id,
                    Edge.target_id == node_id
                )
            )
        )
        
        if max_hops is not None:
            # For now, we'll get direct connections only
            # TODO: Implement multi-hop traversal with temporal constraints
            pass
        
        edges = edge_query.all()
        
        # Separate inbound and outbound edges
        inbound_edges = [e for e in edges if e.target_id == node_id]
        outbound_edges = [e for e in edges if e.source_id == node_id]
        
        # Get connected nodes
        connected_node_ids = set()
        for edge in edges:
            if edge.source_id != node_id:
                connected_node_ids.add(edge.source_id)
            if edge.target_id != node_id:
                connected_node_ids.add(edge.target_id)
        
        connected_nodes = self.session.query(Node).filter(
            Node.id.in_(connected_node_ids)
        ).all()
        
        return {
            "node": node,
            "inbound_edges": inbound_edges,
            "outbound_edges": outbound_edges,
            "connected_nodes": connected_nodes,
            "temporal_filter": {
                "start_date": self.start_date,
                "end_date": self.end_date
            }
        }
    
    def find_connected_nodes(self, node_id: uuid.UUID, max_hops: int = 1) -> List[Node]:
        """Find all nodes connected to a given node within the temporally-filtered graph."""
        # Always include the base node in valid nodes, regardless of filters
        valid_node_ids = self._get_valid_node_ids(base_node_id=node_id)
        valid_edge_ids = self._get_valid_edge_ids()
        
        # Use BFS to find connected nodes
        visited = {node_id}
        queue = [(node_id, 0)]
        connected_node_ids = set()
        
        while queue:
            current_node_id, hop_distance = queue.pop(0)
            
            if max_hops is not None and hop_distance >= max_hops:
                continue
            
            # Find edges connected to current node
            edge_query = self.session.query(Edge).filter(
                and_(
                    Edge.id.in_(valid_edge_ids),
                    or_(
                        Edge.source_id == current_node_id,
                        Edge.target_id == current_node_id
                    )
                )
            )
            
            for edge in edge_query.all():
                # Get the other node in the edge
                other_node_id = edge.target_id if edge.source_id == current_node_id else edge.source_id
                
                if other_node_id not in visited:
                    visited.add(other_node_id)
                    connected_node_ids.add(other_node_id)
                    queue.append((other_node_id, hop_distance + 1))
        
        # Return the connected nodes
        if connected_node_ids:
            return self.session.query(Node).filter(Node.id.in_(connected_node_ids)).all()
        return []
    
    def get_temporal_stats(self) -> Dict[str, Any]:
        """Get statistics about the temporally-filtered graph."""
        valid_node_ids = self._get_valid_node_ids()
        valid_edge_ids = self._get_valid_edge_ids()
        
        total_nodes = self.session.query(Node).count()
        total_edges = self.session.query(Edge).count()
        
        return {
            "filters": {
                "start_date": self.start_date,
                "end_date": self.end_date,
                "node_types": self.node_types,
                "relationship_types": self.relationship_types
            },
            "filtered_counts": {
                "nodes": len(valid_node_ids),
                "edges": len(valid_edge_ids)
            },
            "total_counts": {
                "nodes": total_nodes,
                "edges": total_edges
            },
            "filter_ratio": {
                "nodes": len(valid_node_ids) / total_nodes if total_nodes > 0 else 0,
                "edges": len(valid_edge_ids) / total_edges if total_edges > 0 else 0
            }
        }


# NOTE (2026-07-08 KG audit G5): sixteen zero-caller graph-algorithm
# functions were deleted here (shortest-path/subgraph/degree family,
# integrity+count helpers, get_or_create_node, update_edge_type,
# delete_edges, find_similar_nodes_by_neighbors, the temporal-range
# builders). Reachability-checked against the live roots before removal;
# git history has the bodies if a graph algorithm is ever wanted back.
