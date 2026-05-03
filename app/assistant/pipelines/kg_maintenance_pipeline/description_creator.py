"""
description_creator.py — core logic for generating node descriptions via LLM.

Called by step_description_fill.  Separated here so the agent runner is
testable in isolation from the pipeline step.

Session discipline mirrors the original archived implementation:
  - Read phase: short-lived session, closed before any LLM call.
  - LLM phase:  no session open.
  - Write phase: short-lived session per node.

Edge pre-filtering
------------------
Nodes with more than EDGE_PREFILTER_THRESHOLD edges are pre-filtered using a
cheap nano model that selects the ~150-200 most valuable edges before the full
description_creator processes them in batches.  This reduces Jukka's 1,827
edges from ~91 batches to ~10.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.models.base import get_session

logger = get_logger(__name__)

BATCH_SIZE = 20  # edges per description LLM call
EDGE_PREFILTER_THRESHOLD = 60  # only pre-filter nodes above this edge count
EDGE_FILTER_BATCH_SIZE = 200  # edges per nano pre-filter call

# Edge types ranked by signal quality for sorting before batching.
# Lower rank = higher priority (appears in earlier batches).
_EDGE_TYPE_PRIORITY = {
    "is_married_to": 0, "is_spouse_of": 0, "is_wife_of": 0, "is_husband_of": 0,
    "is_parent_of": 0, "is_child_of": 0, "is_father_of": 0, "is_mother_of": 0,
    "is_sibling_of": 0, "is_brother_of": 0, "is_sister_of": 0,
    "is_friend_of": 1, "is_colleague_of": 1, "works_with": 1,
    "works_at": 2, "employed_by": 2, "is_member_of": 2, "affiliated_with": 2,
    "attended": 2, "graduated_from": 2, "studied_at": 2,
    "lives_in": 3, "located_in": 3, "born_in": 3, "hometown": 3,
    "owns": 4, "has_pet": 4, "authored": 4, "created": 4,
    "has_state": 5, "has_event": 5, "has_goal": 5,
    "interested_in": 6, "hobby": 6, "plays": 6, "practices": 6,
}
_DEFAULT_EDGE_PRIORITY = 10


def _edge_sort_key(edge: Dict[str, Any]) -> tuple:
    """Sort edges: sentence-bearing first, then by edge type importance."""
    has_sentence = 0 if edge.get("sentence") else 1
    edge_type = (edge.get("edge_type") or "").lower()
    type_rank = _EDGE_TYPE_PRIORITY.get(edge_type, _DEFAULT_EDGE_PRIORITY)
    return (has_sentence, type_rank, edge_type)


def _format_edge_oneliner(edge: Dict[str, Any], index: int) -> str:
    """Format an edge as a compact line for the nano pre-filter."""
    direction = (edge.get("direction") or "?").upper()
    edge_type = edge.get("edge_type") or "unknown"
    connected = edge.get("connected_node") or {}
    label = connected.get("label") or "?"
    node_type = connected.get("type") or "?"
    line = f"{index}. {direction}: {edge_type} -> {label} ({node_type})"
    sentence = (edge.get("sentence") or "").strip()
    if sentence:
        line += f" | \"{sentence[:120]}\""
    return line


def _prefilter_edges(
    all_edges: List[Dict[str, Any]],
    node_label: str,
    node_type: str,
) -> List[Dict[str, Any]]:
    """
    For high-degree nodes, use a cheap LLM to select the most valuable edges.
    Nodes with <= EDGE_PREFILTER_THRESHOLD edges skip this step.
    """
    if len(all_edges) <= EDGE_PREFILTER_THRESHOLD:
        return all_edges

    logger.info(
        "[edge_prefilter] %s has %d edges — running nano pre-filter",
        node_label, len(all_edges),
    )
    edge_filter_agent = DI.agent_factory.create_agent("kg_maintenance::edge_filter")
    _scope = build_pipeline_scope_context(
        pipeline_id="kg_maintenance_pipeline", actor_id="edge_filter_runner",
    )
    edge_filter_agent.blackboard.update_state_value("scope_context", _scope)
    keep_global: set[int] = set()

    index_batches = _chunk_edges(
        list(range(len(all_edges))), EDGE_FILTER_BATCH_SIZE,
    )

    for batch_num, global_indices in enumerate(index_batches, 1):
        lines = []
        for local_i, global_i in enumerate(global_indices, 1):
            lines.append(_format_edge_oneliner(all_edges[global_i], local_i))

        try:
            response = edge_filter_agent.action_handler(
                Message(agent_input={
                    "node_label": node_label,
                    "node_type": node_type,
                    "edge_list": "\n".join(lines),
                })
            )
            result = (response.data if response and hasattr(response, "data") else None) or {}
            for local_idx in result.get("keep_indices", []):
                if isinstance(local_idx, int) and 1 <= local_idx <= len(global_indices):
                    keep_global.add(global_indices[local_idx - 1])
        except Exception:
            logger.debug(
                "[edge_prefilter] Batch %d failed for %s — keeping all edges in batch",
                batch_num, node_label, exc_info=True,
            )
            keep_global.update(global_indices)

    filtered = [all_edges[i] for i in sorted(keep_global)]
    logger.info(
        "[edge_prefilter] %s: %d -> %d edges (%.0f%% reduction)",
        node_label, len(all_edges), len(filtered),
        (1 - len(filtered) / len(all_edges)) * 100 if all_edges else 0,
    )
    return filtered


def generate_description_for_node(node_id: str) -> str | None:
    """
    Generate a description for a single node.

    Returns the description string, or None if the node cannot be loaded or
    the LLM returns nothing.  Raises on LLM or write failure so the caller
    can decide whether to abort or continue.
    """
    description_agent = DI.agent_factory.create_agent("kg_maintenance::description_creator")
    _scope = build_pipeline_scope_context(
        pipeline_id="kg_maintenance_pipeline", actor_id="description_creator_runner",
    )
    description_agent.blackboard.update_state_value("scope_context", _scope)

    # ── READ PHASE ────────────────────────────────────────────────────────────
    data_session = get_session()
    try:
        from app.assistant.kg_core.kg_utils.kg_tools import inspect_node_neighborhood
        node_info, all_edges = inspect_node_neighborhood(node_id, data_session)
    except Exception:
        logger.debug("[description_creator] Failed to load node %s", node_id, exc_info=True)
        raise
    finally:
        data_session.close()

    # ── EDGE PRE-FILTER (nano) ────────────────────────────────────────────────
    edges = _prefilter_edges(all_edges, node_info["label"], node_info["type"])

    # ── EDGE SORT ─────────────────────────────────────────────────────────────
    edges.sort(key=_edge_sort_key)

    # ── LLM PHASE ─────────────────────────────────────────────────────────────
    chunks = _chunk_edges(edges, BATCH_SIZE)
    description = ""
    for i, chunk in enumerate(chunks):
        partial_neighborhood = {"node": node_info, "edges": chunk}
        agent_input = {
            "node_label": node_info["label"],
            "node_type": node_info["type"],
            "node_aliases": node_info["aliases"],
            "node_attributes": node_info["attributes"],
            "neighborhood_data": json.dumps(partial_neighborhood),
            "prior_description": description if (i > 0 and description) else "",
        }
        response = description_agent.action_handler(Message(agent_input=agent_input))
        data = response.data if response and hasattr(response, "data") else {}
        new_description = (data or {}).get("new_description")
        if new_description:
            description = new_description

    return description or None


def persist_description(node_id: str, description: str) -> bool:
    """
    Write description to the node row.  Returns True if the row was actually
    changed, False if the value was identical.

    Description is a downstream projection of the node's KG neighborhood
    (currently filled by either this maintenance pipeline or the wiki lead),
    not a structural property of the node itself. Writing it MUST NOT bump
    Node.updated_at — otherwise change_detection.find_changed_neighborhood_nodes
    sees the node as freshly modified and triggers a wiki refresh on every
    page that links to it, which in turn rewrites their leads, which bumps
    THEIR updated_at, cascading without bound.

    Implementation: Core UPDATE with `updated_at = Node.updated_at` self-
    reference. Explicitly providing a value for a column suppresses its
    `onupdate=func.now()` hook, so the timestamp is preserved verbatim.
    """
    from sqlalchemy import update
    from app.assistant.kg.db.knowledge_graph_db import Node

    write_session = get_session()
    try:
        current = (
            write_session.query(Node.description)
            .filter(Node.id == node_id)
            .scalar()
        )
        if current is None and not write_session.query(Node.id).filter(Node.id == node_id).scalar():
            logger.debug("[description_creator] Node %s not found at write time", node_id)
            return False
        if description == current:
            return False
        write_session.execute(
            update(Node)
            .where(Node.id == node_id)
            .values(description=description, updated_at=Node.updated_at)
        )
        write_session.commit()
        return True
    except Exception:
        write_session.rollback()
        logger.debug("[description_creator] Write failed node=%s", node_id, exc_info=True)
        raise
    finally:
        write_session.close()


def _chunk_edges(edges: List[Any], batch_size: int) -> List[List[Any]]:
    return [edges[i : i + batch_size] for i in range(0, len(edges), batch_size)]
