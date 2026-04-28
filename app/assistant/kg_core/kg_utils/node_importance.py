"""
Shared utility for identifying "important" KG nodes.

Used by both the description-fill pipeline and the entity-card pipeline.

Algorithm
---------
1. **Structural gate** – only nodes of eligible types with sufficient edges.
   Entity nodes need >= MIN_EDGES_ENTITY; others need >= 1.
2. **Priority scoring** – blended importance × weight + pagerank × weight.
3. **Nano node filter** (optional) – cheap LLM pass to drop obvious junk.
"""
from __future__ import annotations

from typing import NamedTuple, Set

from sqlalchemy import func, union_all
from sqlalchemy.orm import Session

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

logger = get_logger(__name__)

IMPORTANCE_WEIGHT = 0.4
PAGERANK_WEIGHT = 0.6
DEFAULT_SCORE = 0.5
ELIGIBLE_NODE_TYPES = {"Entity", "State", "Event", "Goal", "Property"}
MIN_EDGES_ENTITY = 3
NANO_FILTER_BATCH_SIZE = 50


class NodeSnapshot(NamedTuple):
    node_id: str
    label: str
    node_type: str
    importance: float
    pagerank: float
    edge_count: int


def _query_structural_gate(session: Session) -> list[NodeSnapshot]:
    """Return nodes that pass the structural gate (type + min edges)."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    in_refs = session.query(Edge.target_id.label("node_id"))
    out_refs = session.query(Edge.source_id.label("node_id"))
    all_refs = union_all(in_refs, out_refs).subquery()
    edge_counts = (
        session.query(
            all_refs.c.node_id,
            func.count().label("edge_count"),
        )
        .group_by(all_refs.c.node_id)
        .subquery()
    )

    rows = (
        session.query(
            Node.id, Node.label, Node.node_type,
            Node.importance, Node.pagerank_score,
            edge_counts.c.edge_count,
        )
        .join(edge_counts, Node.id == edge_counts.c.node_id)
        .filter(
            Node.node_type.in_(ELIGIBLE_NODE_TYPES),
            (Node.node_type != "Entity") | (edge_counts.c.edge_count >= MIN_EDGES_ENTITY),
        )
        .all()
    )

    return [
        NodeSnapshot(
            node_id=str(r.id),
            label=r.label or "",
            node_type=r.node_type or "",
            importance=float(r.importance if r.importance is not None else DEFAULT_SCORE),
            pagerank=float(r.pagerank_score if r.pagerank_score is not None else DEFAULT_SCORE),
            edge_count=int(r.edge_count),
        )
        for r in rows
    ]


def _run_nano_node_filter(snapshots: list[NodeSnapshot]) -> list[NodeSnapshot]:
    """
    Send candidate node labels to kg_maintenance::description_filter in
    batches.  Returns only the KEEP-approved candidates.
    """
    if not snapshots:
        return snapshots

    try:
        filter_agent = DI.agent_factory.create_agent("kg_maintenance::description_filter")
        _scope = build_pipeline_scope_context(
            pipeline_id="kg_maintenance_pipeline", actor_id="description_filter_runner",
        )
        filter_agent.blackboard.update_state_value("scope_context", _scope)
    except Exception:
        logger.debug("Failed to create description_filter agent — keeping all candidates", exc_info=True)
        return snapshots

    approved: list[NodeSnapshot] = []

    for batch_start in range(0, len(snapshots), NANO_FILTER_BATCH_SIZE):
        batch = snapshots[batch_start : batch_start + NANO_FILTER_BATCH_SIZE]

        numbered_lines = []
        for local_i, snap in enumerate(batch, 1):
            numbered_lines.append(f"{local_i}. {snap.label} ({snap.node_type}, {snap.edge_count} edges)")

        batch_num = batch_start // NANO_FILTER_BATCH_SIZE + 1
        try:
            response = filter_agent.action_handler(
                Message(agent_input={"candidate_list": "\n".join(numbered_lines)})
            )
            result = response.data or {}
            decisions = result.get("decisions", [])

            keep_indices: set[int] = set()
            for d in decisions:
                num = d.get("number") if isinstance(d, dict) else getattr(d, "number", None)
                verdict = (
                    d.get("verdict", "") if isinstance(d, dict) else getattr(d, "verdict", "")
                )
                if str(verdict).strip().upper() == "KEEP" and num is not None:
                    keep_indices.add(int(num))

            for local_i, snap in enumerate(batch, 1):
                if local_i in keep_indices:
                    approved.append(snap)

            logger.info(
                "│  nano filter batch %d: %d -> %d", batch_num, len(batch), len(keep_indices),
            )
        except Exception:
            logger.debug(
                "[nano_node_filter] Batch %d failed — keeping all nodes in batch",
                batch_num, exc_info=True,
            )
            approved.extend(batch)

    return approved


def get_important_nodes(
    session: Session,
    *,
    run_nano_filter: bool = True,
    max_nodes: int | None = None,
) -> list[NodeSnapshot]:
    """
    Identify important KG nodes using structural gate + priority scoring +
    optional nano LLM filter.

    Returns a list of NodeSnapshot sorted by blended priority (descending).
    """
    snapshots = _query_structural_gate(session)

    snapshots.sort(
        key=lambda s: IMPORTANCE_WEIGHT * s.importance + PAGERANK_WEIGHT * s.pagerank,
        reverse=True,
    )

    logger.info(
        "│  structural gate: %d candidates (Entity >= %d edges)",
        len(snapshots), MIN_EDGES_ENTITY,
    )

    if run_nano_filter:
        snapshots = _run_nano_node_filter(snapshots)
        logger.info("│  nano filter: %d approved", len(snapshots))

    if max_nodes is not None:
        snapshots = snapshots[:max_nodes]

    return snapshots


def get_important_node_ids(
    session: Session,
    *,
    run_nano_filter: bool = True,
    max_nodes: int | None = None,
) -> Set[str]:
    """Convenience wrapper that returns just the node IDs as a set."""
    return {s.node_id for s in get_important_nodes(
        session, run_nano_filter=run_nano_filter, max_nodes=max_nodes,
    )}
