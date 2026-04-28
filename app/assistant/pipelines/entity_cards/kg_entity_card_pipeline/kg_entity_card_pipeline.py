"""
Simple Entity Card Pipeline
Creates entity cards for each node in the knowledge graph using existing tools
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set, Iterator, Tuple
from app.models.base import get_session
from app.assistant.kg.db.knowledge_graph_db import Node, Edge
from app.assistant.kg_core.kg_utils.kg_tools import inspect_node_neighborhood
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.assistant.entity_management.entity_cards import (
    create_entity_card, 
    get_entity_card_by_name, 
    get_entity_card_stats,
    initialize_entity_cards_db,
    EntityCard,
    rebuild_entity_card_index_for_card,
)
from app.assistant.kg_core.user_identity import PRIMARY_USER_NODE_LABEL
from app.models.maintenance_logs import (
    get_last_maintenance_run_time,
    log_maintenance_run,
    get_nodes_updated_since
)
from app.assistant.utils.pydantic_classes import Message
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

logger = get_logger(__name__)

BATCH_SIZE = 20  # match description_creator for consistent iterative refinement

from app.assistant.pipelines.kg_maintenance_pipeline.description_creator import (
    _prefilter_edges,
    _edge_sort_key,
)
from app.assistant.kg_core.kg_utils.node_importance import get_important_node_ids

import threading as _threading

_group_a_lock = _threading.Lock()
_group_a_cache: Optional[Set[str]] = None

GROUP_A_PAGERANK_STATE = 0.002
GROUP_A_PAGERANK_OTHER = 0.005


def _get_group_a_ids() -> Set[str]:
    """
    Compute Group A node IDs once and cache.  Group A = nodes important enough
    that edges to them carry signal worth analyzing.

    Split threshold: State nodes >= 0.002 (to preserve contact info),
    Entity/Event/Goal >= 0.005.
    """
    global _group_a_cache
    with _group_a_lock:
        if _group_a_cache is not None:
            return _group_a_cache

        from sqlalchemy import union_all
        session = get_session()
        try:
            in_refs = session.query(Edge.target_id.label("node_id"))
            out_refs = session.query(Edge.source_id.label("node_id"))
            all_refs = union_all(in_refs, out_refs).subquery()
            ec = (
                session.query(all_refs.c.node_id, func.count().label("ec"))
                .group_by(all_refs.c.node_id).subquery()
            )
            rows = (
                session.query(Node.id, Node.node_type, Node.pagerank_score)
                .join(ec, Node.id == ec.c.node_id)
                .filter(Node.node_type.in_(["Entity", "State", "Event", "Goal"]))
                .all()
            )
            ids: Set[str] = set()
            for r in rows:
                thr = GROUP_A_PAGERANK_STATE if r.node_type == "State" else GROUP_A_PAGERANK_OTHER
                if (r.pagerank_score or 0) >= thr:
                    ids.add(str(r.id))
            _group_a_cache = ids
            logger.info("Group A cached: %d nodes", len(ids))
            return ids
        finally:
            session.close()


def _filter_edges_to_group_a(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only edges whose connected node is in Group A."""
    group_a = _get_group_a_ids()
    return [e for e in edges if e.get("connected_node", {}).get("id") in group_a]

# ---------------------------------------------------------------------------
# Two-pass influence scoring (kept for backward compat / report tooling)
# ---------------------------------------------------------------------------

import math as _math
from collections import defaultdict as _defaultdict


def _load_all_edge_pairs(session) -> List[Tuple[str, str]]:
    """Single query returning every (source_id, target_id) pair in the graph."""
    rows = session.query(Edge.source_id, Edge.target_id).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def _compute_degree(edge_pairs: List[Tuple[str, str]]) -> Dict[str, int]:
    """Pass 1: total edge count (in + out) per node."""
    degree: Dict[str, int] = {}
    for src, tgt in edge_pairs:
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1
    return degree


def _compute_influence(
    edge_pairs: List[Tuple[str, str]],
    degree: Dict[str, int],
) -> Dict[str, float]:
    """Pass 2: for each node, sum the degrees of all its direct neighbors."""
    influence: Dict[str, float] = {}
    for src, tgt in edge_pairs:
        influence[src] = influence.get(src, 0.0) + degree.get(tgt, 0)
        influence[tgt] = influence.get(tgt, 0.0) + degree.get(src, 0)
    return influence


def _adjusted_influence(raw_influence: float, label: str) -> float:
    """Apply a sqrt word-count penalty: score / sqrt(word_count)."""
    words = max(1, len((label or "").strip().split()))
    return raw_influence / _math.sqrt(words)


# ---------------------------------------------------------------------------
# Neighborhood diversity scoring (pre-LLM selection gate)
# ---------------------------------------------------------------------------
# Primary selection signal: diversity of a node's connections, not raw volume.
# A card-worthy entity has diverse relationship types, connects to other
# named entities, and appears across multiple time periods.

def _compute_neighborhood_diversity(session) -> Dict[str, dict]:
    """
    For each node compute structural diversity metrics in a single DB pass.

    Returns {node_id: {
        "distinct_rel_types": int,
        "distinct_entity_neighbors": int,
        "distinct_weeks": int,
    }}.
    """
    rows = session.query(
        Edge.source_id,
        Edge.target_id,
        Edge.relationship_type,
        Edge.original_message_timestamp,
        Edge.created_at,
    ).all()

    node_type_rows = session.query(Node.id, Node.node_type).all()
    node_type_map = {str(r[0]): (r[1] or "").strip() for r in node_type_rows}

    stats: Dict[str, dict] = _defaultdict(lambda: {
        "rel_types": set(),
        "entity_neighbors": set(),
        "weeks": set(),
    })

    for src_raw, tgt_raw, rel_type, msg_ts, created_ts in rows:
        src = str(src_raw)
        tgt = str(tgt_raw)
        rel = (rel_type or "").strip().lower()
        ts = msg_ts or created_ts
        week_key = (ts.isocalendar()[0], ts.isocalendar()[1]) if ts else None

        for nid, neighbor_id in ((src, tgt), (tgt, src)):
            s = stats[nid]
            if rel:
                s["rel_types"].add(rel)
            if node_type_map.get(neighbor_id) == "Entity":
                s["entity_neighbors"].add(neighbor_id)
            if week_key:
                s["weeks"].add(week_key)

    return {
        nid: {
            "distinct_rel_types": len(s["rel_types"]),
            "distinct_entity_neighbors": len(s["entity_neighbors"]),
            "distinct_weeks": len(s["weeks"]),
        }
        for nid, s in stats.items()
    }


def _diversity_score(diversity: dict) -> float:
    """
    Composite diversity score: 8*rel_types + 6*entity_neighbors + 4*weeks.
    """
    return (
        diversity.get("distinct_rel_types", 0) * 8.0
        + diversity.get("distinct_entity_neighbors", 0) * 6.0
        + diversity.get("distinct_weeks", 0) * 4.0
    )


DEFAULT_MIN_DIVERSITY_SCORE = 14.0  # kept for report tooling

DEFAULT_MIN_PAGERANK = 0.005


def _format_edge(rel: Dict[str, Any], *, i: int) -> Optional[str]:
    """
    Format a single edge dict defensively. Returns None if malformed.
    """
    if not isinstance(rel, dict):
        return None
    direction = (rel.get("direction") or "").title() or "?"
    edge_type = rel.get("edge_type") or rel.get("relationship_type") or "unknown"
    connected = rel.get("connected_node") if isinstance(rel.get("connected_node"), dict) else {}
    label = connected.get("label")
    ctype = connected.get("type") or connected.get("node_type") or "unknown"
    if not label:
        return None

    rel_text = f"{i}. {direction} relationship: {edge_type}\n"
    rel_text += f"   - Connected to: {label} ({ctype})"

    # For contact information, show the actual value clearly
    if edge_type in ["has_phone", "has_email"]:
        rel_text += f"\n   - Value: {label}"

    desc_full = connected.get("description")
    if isinstance(desc_full, str) and desc_full.strip():
        # Do not truncate descriptions here; the entity-card agent relies on full connected-node
        # context (especially when intermediate State nodes carry the key user-relevance signal).
        rel_text += f"\n   - Description: {desc_full}"

    # Include original evidence text for salience (one-off mention vs recurring/important).
    sentence = rel.get("sentence")
    if isinstance(sentence, str) and sentence.strip():
        rel_text += f"\n   - Evidence sentence: {sentence.strip()}"

    relationship_descriptor = rel.get("relationship_descriptor")
    if isinstance(relationship_descriptor, str) and relationship_descriptor.strip():
        rel_text += f"\n   - Relationship descriptor: {relationship_descriptor.strip()}"

    # Lightweight timestamp (if available) so the LLM can infer recency/one-off nature.
    edge_attrs = rel.get("edge_attributes")
    if isinstance(edge_attrs, dict):
        ts = (
            edge_attrs.get("original_message_timestamp")
            or edge_attrs.get("message_timestamp")
            or edge_attrs.get("provenance_timestamp")
            or edge_attrs.get("timestamp")
        )
        if ts:
            rel_text += f"\n   - Evidence timestamp: {ts}"
    return rel_text


def _format_relationships_data(
    *,
    edges_chunk: List[Dict[str, Any]],
    total_edges: int,
    batch_number: int,
    total_batches: int,
    start_index: int,
) -> str:
    """
    Format a chunk of edges as a prompt-friendly string.
    start_index is the 1-based index offset into the full edge list.
    """
    relationships_text: List[str] = []
    for j, rel in enumerate(edges_chunk):
        line = _format_edge(rel, i=start_index + j)
        if line:
            relationships_text.append(line)

    header = (
        f"Batch {batch_number}/{total_batches} | "
        f"Edges in this batch: {len(edges_chunk)} | "
        f"Total relationships: {total_edges}"
    )
    if not relationships_text:
        return header + "\n" + "No relationships in this batch."
    return header + "\n" + "\n".join(relationships_text)


def _query_nodes_with_edges(session: Session, min_outgoing_edges: int) -> "Any":
    """
    Return a SQLAlchemy query yielding Node rows matching edge-count criteria.
    """
    if min_outgoing_edges == 0:
        return session.query(Node)
    if min_outgoing_edges == 1:
        return session.query(Node).join(Edge, Node.id == Edge.source_id).distinct()
    # Count outgoing edges and filter
    return (
        session.query(Node)
        .join(Edge, Node.id == Edge.source_id)
        .group_by(Node.id)
        .having(func.count(Edge.id) >= min_outgoing_edges)
    )


def _iter_nodes(
    *,
    session: Session,
    incremental_nodes: Optional[List[Node]],
    min_outgoing_edges: int,
    only_missing_cards: bool,
) -> Tuple[Iterator[Node], int]:
    """
    Return (iterator, total_estimate) for nodes to process.

    For non-incremental runs, prefer streaming from the DB instead of loading all nodes.
    """
    if incremental_nodes is not None:
        nodes = incremental_nodes
        if only_missing_cards:
            nodes = filter_nodes_missing_active_cards(session, nodes)
        return iter(nodes), len(nodes)

    q = _query_nodes_with_edges(session, min_outgoing_edges)
    if only_missing_cards:
        # Deactivated cards count as "covered" — see filter_nodes_missing_active_cards.
        # A row in entity_cards (active or not) means the user has accepted that
        # there should or should not be a card for this name; the nightly run
        # never re-creates from scratch.
        existing = (
            session.query(EntityCard.entity_name)
            .subquery()
        )
        q = q.filter(~Node.label.in_(existing))

    # Materialize into memory so the read cursor is released before writes.
    # SQLite (even WAL mode) blocks writes while a streaming cursor holds a
    # read transaction on the same connection.
    nodes = q.all()
    return iter(nodes), len(nodes)


_CRITIC_BATCH_SIZE = 75


def _run_critic_gate(
    candidates: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], int]:
    """
    Send candidate labels to the entity_card_critic agent in batches.
    Returns (approved_candidates, total_rejected_count).
    """
    if not candidates:
        return candidates, 0

    logger.info("Critic gate: reviewing %d candidate labels (batch size %d)", len(candidates), _CRITIC_BATCH_SIZE)

    try:
        critic_agent = DI.agent_factory.create_agent("entity_card_critic")
        _scope = build_pipeline_scope_context(
            pipeline_id="entity_cards", actor_id="entity_card_critic_runner",
        )
        critic_agent.blackboard.update_state_value("scope_context", _scope)
    except Exception:
        logger.error("Failed to create critic agent — keeping all candidates")
        logger.debug("critic agent creation exception", exc_info=True)
        return candidates, 0

    global_reject_indices: set[int] = set()

    for batch_start in range(0, len(candidates), _CRITIC_BATCH_SIZE):
        batch = candidates[batch_start : batch_start + _CRITIC_BATCH_SIZE]
        numbered_lines = []
        for local_i, (_nid, label) in enumerate(batch, 1):
            numbered_lines.append(f"{local_i}. {label}")
        candidate_text = "\n".join(numbered_lines)

        batch_num = batch_start // _CRITIC_BATCH_SIZE + 1
        total_batches = (len(candidates) + _CRITIC_BATCH_SIZE - 1) // _CRITIC_BATCH_SIZE
        logger.info("  Critic batch %d/%d (%d labels)", batch_num, total_batches, len(batch))

        try:
            response = critic_agent.action_handler(
                Message(agent_input={"candidate_list": candidate_text})
            )

            if not response or not response.data:
                logger.warning("  Critic batch %d returned no data — keeping batch", batch_num)
                continue

            decisions = response.data.get("decisions", [])
            if not decisions:
                logger.warning("  Critic batch %d returned empty decisions — keeping batch", batch_num)
                continue

            for d in decisions:
                num = d.get("number") if isinstance(d, dict) else getattr(d, "number", None)
                verdict = (
                    d.get("verdict", "") if isinstance(d, dict) else getattr(d, "verdict", "")
                )
                reason = (
                    d.get("reason", "") if isinstance(d, dict) else getattr(d, "reason", "")
                )
                if str(verdict).strip().upper() == "REJECT" and num is not None:
                    global_idx = batch_start + int(num)
                    global_reject_indices.add(global_idx)
                    local_label = batch[int(num) - 1][1] if int(num) <= len(batch) else "?"
                    logger.info("    REJECT #%d %s — %s", global_idx, local_label, reason)

        except Exception:
            logger.error("  Critic batch %d failed — keeping batch", batch_num)
            logger.debug("critic batch %d exception", batch_num, exc_info=True)

    approved = [
        c for i, c in enumerate(candidates, 1) if i not in global_reject_indices
    ]
    return approved, len(global_reject_indices)


def _is_obviously_low_value_label(label: str) -> bool:
    """
    Cheap heuristic filter to avoid generating prompt-injection cards for clearly low-value artifacts.
    This is intentionally conservative; the LLM still makes the final keep/reject decision.
    """
    if not label or not isinstance(label, str):
        return True
    s = label.strip()
    if not s:
        return True
    low = s.lower()

    # Generic relational/role terms — these describe a relationship to the user, not a
    # specific identifiable entity. The actual person should be a named node.
    _GENERIC_ROLES = {
        "friend", "boss", "teacher", "coworker", "someone", "something",
        "colleague", "manager", "supervisor", "employee", "partner", "associate",
        "neighbor", "acquaintance", "contact", "person", "user", "client", "customer",
        "member", "owner", "admin", "guest",
    }
    if low in _GENERIC_ROLES:
        return True

    # Common conversational/action words that produce zero injection value.
    _GENERIC_NOUNS = {
        "meeting", "call", "email", "work", "home", "task", "note", "update",
        "message", "chat", "conversation", "discussion", "issue", "problem",
        "thing", "stuff", "item", "event", "activity", "action", "request",
        "question", "answer", "idea", "plan", "goal", "project", "topic",
        "reminder", "follow-up", "follow up", "check-in", "check in",
    }
    if low in _GENERIC_NOUNS:
        return True

    # Pronouns and generic descriptors.
    _PRONOUNS = {"he", "she", "they", "it", "we", "i", "me", "him", "her", "them", "us"}
    if low in _PRONOUNS:
        return True

    # Short generic phrases: "my friend", "a meeting", "the boss", etc.
    _ARTICLES = {"a ", "an ", "the ", "my ", "our ", "your ", "their ", "his ", "her "}
    if any(low.startswith(art) for art in _ARTICLES):
        remainder = low.split(" ", 1)[-1].strip()
        if remainder in _GENERIC_ROLES or remainder in _GENERIC_NOUNS:
            return True

    # Game/inventory-like artifacts that have historically produced bad cards.
    if "ammunition" in low or low.endswith(" ammo") or " ammo" in low or "acp" in low:
        # Keep this narrow; we're only trying to avoid the most egregious known class.
        if any(ch.isdigit() for ch in low) or low.startswith("."):
            return True

    # Avoid pure numeric / punctuation-heavy labels.
    alnum = sum(1 for ch in s if ch.isalnum())
    if alnum <= 1:
        return True

    return False


def store_entity_card_in_db(
    session,
    entity_name: str,
    entity_card: Dict[str, Any],
    source_node_id=None,
    node_info=None,
    *,
    unique_connected_nodes: int | None = None,
    commit: bool = True,
    force: bool = False,
):
    def _normalize_meta_to_dict(meta_raw) -> dict:
        import json
        if not meta_raw:
            return {}
        loaded = meta_raw
        if isinstance(meta_raw, str):
            try:
                loaded = json.loads(meta_raw)
            except Exception:
                return {}
        if isinstance(loaded, dict):
            return {str(k): loaded[k] for k in loaded.keys()}
        if isinstance(loaded, list):
            out = {}
            for item in loaded:
                if isinstance(item, dict) and "key" in item:
                    out[str(item.get("key"))] = item.get("value")
            return out
        return {}

    def _build_card_metadata_dict(
        *,
        entity_name: str,
        entity_type: str,
        summary: str,
        key_facts: list,
        relationships: list,
        aliases: list,
        original_aliases: list,
        contact_info: list | None = None,
        user_relevance_reason: str | None = None,
        node_info: dict | None = None,
        source_node_id,
        unique_connected_nodes: int | None,
        legacy_meta: dict,
    ) -> dict:
        from datetime import datetime, timezone

        def _truncate_sentenceish(text: str, max_len: int) -> str:
            s = (text or "").strip()
            if len(s) <= max_len:
                return s
            cut = s[:max_len]
            last_period = cut.rfind(".")
            if last_period > 40:
                return cut[: last_period + 1].strip()
            return cut.strip() + "..."

        now = datetime.now(timezone.utc).isoformat()
        ci = contact_info if isinstance(contact_info, list) else []
        urr = (user_relevance_reason or "").strip() or None

        l0 = {
            "user_relevance_reason": urr,
            "last_updated_utc": now,
        }
        l1 = {
            "summary": _truncate_sentenceish(summary, 400),
            "contact_info": ci[:6],
            "key_facts": (key_facts or [])[:5],
            "relationships": (relationships or [])[:3],
            "last_updated_utc": now,
        }
        l2 = {
            "summary": (summary or "").strip(),
            "contact_info": ci,
            "key_facts": (key_facts or [])[:10],
            "relationships": (relationships or [])[:10],
            "aliases": (aliases or [])[:20],
            "original_aliases": (original_aliases or [])[:20],
            "last_updated_utc": now,
        }

        meta = {
            "schema_name": "emi_entity_card_metadata",
            "schema_version": 2,
            "contact_info": ci,
            "views": {
                "level0": l0,
                "level1": l1,
                "level2": l2,
            },
            "gating": {
                "inject_policy": {
                    "default_level": 1,
                    "max_auto_level": 1,
                }
            },
            "signals": {
                "graph": {
                    "unique_connected_nodes": unique_connected_nodes,
                }
            },
            "provenance": {
                "source_node_id": str(source_node_id) if source_node_id is not None else None,
                "node_type": (node_info or {}).get("type"),
                "generated_at_utc": now,
                "legacy_meta": legacy_meta or {},
            },
        }
        return meta

    try:
        existing_card = get_entity_card_by_name(session, entity_name)

        current_description = node_info.get("description") if node_info else None

        if existing_card and not force:
            stored_desc = getattr(existing_card, "original_description", None)
            if current_description is not None and current_description == stored_desc:
                logger.info(
                    "Entity card for %s exists and description unchanged; skipping overwrite.",
                    entity_name,
                )
                return existing_card
            if current_description != stored_desc:
                logger.info(
                    "Entity card for %s: KG description changed — regenerating.", entity_name,
                )

        original_description = current_description
        original_aliases = node_info.get('aliases', []) if node_info else []

        # Build structured card_metadata (dict), keeping any legacy key/value metadata.
        legacy_meta_dict = _normalize_meta_to_dict(entity_card.get("card_metadata", None))

        raw_contact = entity_card.get("contact_info") or []
        contact_info_dicts = []
        for ci_item in raw_contact:
            if isinstance(ci_item, dict):
                contact_info_dicts.append(ci_item)
            elif hasattr(ci_item, "model_dump"):
                contact_info_dicts.append(ci_item.model_dump())

        meta = _build_card_metadata_dict(
            entity_name=entity_name,
            entity_type=entity_card.get("entity_type", "unknown"),
            summary=entity_card.get("summary", ""),
            key_facts=entity_card.get("key_facts", []) or [],
            relationships=entity_card.get("relationships", []) or [],
            aliases=entity_card.get("aliases", []) or [],
            original_aliases=original_aliases or [],
            contact_info=contact_info_dicts,
            user_relevance_reason=entity_card.get("user_relevance_reason"),
            node_info=node_info,
            source_node_id=source_node_id,
            unique_connected_nodes=unique_connected_nodes,
            legacy_meta=legacy_meta_dict,
        )

        if existing_card:
            logger.info(f"Entity card for {entity_name} exists, overwriting")
            existing_card.entity_type = entity_card.get('entity_type', 'unknown')
            existing_card.summary = entity_card.get('summary', '')
            existing_card.source_node_id = source_node_id
            existing_card.original_description = original_description
            existing_card.original_aliases = original_aliases
            existing_card.key_facts = entity_card.get('key_facts', [])
            existing_card.relationships = entity_card.get('relationships', [])
            existing_card.user_relevance_reason = entity_card.get('user_relevance_reason') or None
            existing_card.aliases = entity_card.get('aliases', [])
            existing_card.confidence = entity_card.get('confidence')
            existing_card.batch_number = entity_card.get('batch_number')
            existing_card.total_batches = entity_card.get('total_batches')
            existing_card.card_metadata = meta
        else:
            logger.info(f"Creating new entity card for {entity_name}")
            existing_card = create_entity_card(
                session=session,
                entity_name=entity_name,
                entity_type=entity_card.get('entity_type', 'unknown'),
                summary=entity_card.get('summary', ''),
                source_node_id=source_node_id,
                original_description=original_description,
                original_aliases=original_aliases,
                key_facts=entity_card.get('key_facts', []),
                relationships=entity_card.get('relationships', []),
                user_relevance_reason=entity_card.get('user_relevance_reason') or None,
                aliases=entity_card.get('aliases', []),
                confidence=entity_card.get('confidence'),
                batch_number=entity_card.get('batch_number'),
                total_batches=entity_card.get('total_batches'),
                card_metadata=meta,
            )

        # Keep the lookup index in sync.
        rebuild_entity_card_index_for_card(session, existing_card)

        if commit:
            session.commit()
        return existing_card

    except Exception as e:
        logger.error(f"Error storing entity card for {entity_name}: {e}")
        session.rollback()
        return None
