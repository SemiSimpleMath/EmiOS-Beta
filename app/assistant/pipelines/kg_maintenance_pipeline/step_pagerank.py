"""
Step: pagerank

Computes weighted PageRank over the KG node graph and persists the result
to Node.pagerank_score.

The graph is collapsed before scoring: intermediate State/Event/Goal/Concept
nodes are treated as passthrough. If Entity A connects to State S and State S
connects to Entity B, a virtual edge A→B is created. This ensures that entities
connected through states (e.g. Jukka → Married_to_Katy [State] → Katy) get
proper rank propagation without State nodes absorbing score.

All nodes (including non-Entity) still receive a pagerank score, but the
primary signal is Entity-to-Entity connectivity through the collapsed graph.

Algorithm
---------
Standard power-iteration PageRank with damping=0.85, max 100 iterations,
convergence tolerance 1e-6.  Edge weights come from edge.importance (default
0.5 when NULL).  The graph is treated as undirected for scoring purposes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

DAMPING = 0.85
MAX_ITER = 100
TOLERANCE = 1e-6
DEFAULT_EDGE_WEIGHT = 0.5

# Node types that are treated as passthrough (collapsed) in the graph.
# Edges Entity→State→Entity become virtual Entity→Entity edges.
_PASSTHROUGH_TYPES = frozenset({"State", "Event", "Goal", "Concept", "Property"})


def _collapse_passthrough_edges(
    node_ids: List[str],
    edges: List[Tuple[str, str, float]],
    node_type_map: Dict[str, str],
) -> List[Tuple[str, str, float]]:
    """
    Collapse passthrough nodes to create virtual Entity→Entity edges.

    For every path Entity→Passthrough→Entity, add a virtual edge between
    the two entities with the average weight of the two hops.
    The original edges are also kept so non-Entity nodes still participate.
    """
    # Build adjacency for passthrough expansion.
    neighbors: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for src, tgt, w in edges:
        neighbors[src].append((tgt, w))
        neighbors[tgt].append((src, w))

    entity_ids: Set[str] = {
        nid for nid in node_ids
        if node_type_map.get(nid, "") not in _PASSTHROUGH_TYPES
    }

    virtual_edges: List[Tuple[str, str, float]] = []
    seen_virtual: Set[Tuple[str, str]] = set()

    for passthrough_id in node_ids:
        if node_type_map.get(passthrough_id, "") not in _PASSTHROUGH_TYPES:
            continue

        # Find all entity neighbors of this passthrough node.
        entity_neighbors: List[Tuple[str, float]] = []
        for neighbor_id, w in neighbors.get(passthrough_id, []):
            if neighbor_id in entity_ids:
                entity_neighbors.append((neighbor_id, w))

        # Create virtual edges between all pairs of entity neighbors.
        for i, (eid_a, w_a) in enumerate(entity_neighbors):
            for eid_b, w_b in entity_neighbors[i + 1:]:
                pair = (min(eid_a, eid_b), max(eid_a, eid_b))
                if pair not in seen_virtual:
                    seen_virtual.add(pair)
                    virtual_edges.append((eid_a, eid_b, (w_a + w_b) / 2.0))

    logger.info(
        "[step_pagerank] collapsed %d passthrough nodes into %d virtual entity edges",
        sum(1 for nid in node_ids if node_type_map.get(nid, "") in _PASSTHROUGH_TYPES),
        len(virtual_edges),
    )

    return edges + virtual_edges


def run(ctx: PipelineContext) -> dict:
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    # ── READ PHASE ────────────────────────────────────────────────────────────
    session = get_session()
    try:
        node_rows = session.query(Node.id, Node.node_type).all()
        node_ids = [str(r.id) for r in node_rows]
        node_type_map = {str(r.id): (r.node_type or "").strip() for r in node_rows}

        edge_rows = session.query(
            Edge.source_id, Edge.target_id, Edge.importance
        ).all()
        edges = [
            (str(r.source_id), str(r.target_id), float(r.importance or DEFAULT_EDGE_WEIGHT))
            for r in edge_rows
        ]
    finally:
        session.close()

    if not node_ids:
        logger.debug("[step_pagerank] No nodes found — skipping.")
        return {"nodes_scored": 0, "iterations": 0, "converged": True}

    # Collapse passthrough nodes to boost Entity→Entity connectivity.
    edges = _collapse_passthrough_edges(node_ids, edges, node_type_map)

    # ── PAGERANK COMPUTATION ──────────────────────────────────────────────────
    scores, iterations, converged = _compute_pagerank(node_ids, edges)

    # ── WRITE PHASE (batched) ─────────────────────────────────────────────────
    write_session = get_session()
    try:
        items = list(scores.items())
        chunk_size = 500
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            for node_id, score in chunk:
                write_session.query(Node).filter(Node.id == node_id).update(
                    {"pagerank_score": round(score, 8)},
                    synchronize_session=False,
                )
        write_session.commit()
    except Exception:
        write_session.rollback()
        logger.debug("[step_pagerank] Write failed", exc_info=True)
        raise
    finally:
        write_session.close()

    logger.info(
        "[step_pagerank] scored=%d iterations=%d converged=%s virtual_edges=%d",
        len(scores),
        iterations,
        converged,
        len(edges) - len(edge_rows) if 'edge_rows' in dir() else 0,
    )
    return {"nodes_scored": len(scores), "iterations": iterations, "converged": converged}


def _compute_pagerank(
    node_ids: list[str],
    edges: list[tuple[str, str, float]],
) -> tuple[Dict[str, float], int, bool]:
    """
    Power-iteration PageRank.

    The graph is treated as undirected: each edge (src, tgt, w) contributes
    weight w to both src→tgt and tgt→src adjacency.
    """
    n = len(node_ids)
    id_set = set(node_ids)

    adjacency: Dict[str, Dict[str, float]] = defaultdict(dict)
    for src, tgt, w in edges:
        if src not in id_set or tgt not in id_set:
            continue
        adjacency[src][tgt] = adjacency[src].get(tgt, 0.0) + w
        adjacency[tgt][src] = adjacency[tgt].get(src, 0.0) + w

    out_weight: Dict[str, float] = {}
    for node, neighbors in adjacency.items():
        out_weight[node] = sum(neighbors.values())

    init = 1.0 / n
    scores: Dict[str, float] = {nid: init for nid in node_ids}

    teleport = (1.0 - DAMPING) / n

    converged = False
    iterations = 0
    for iteration in range(1, MAX_ITER + 1):
        new_scores: Dict[str, float] = {nid: teleport for nid in node_ids}

        for src, neighbors in adjacency.items():
            src_score = scores.get(src, init)
            total_out = out_weight.get(src, 0.0)
            if total_out == 0.0:
                continue
            for tgt, w in neighbors.items():
                new_scores[tgt] = new_scores.get(tgt, teleport) + DAMPING * src_score * (w / total_out)

        dangling_sum = sum(
            scores[nid] for nid in node_ids if out_weight.get(nid, 0.0) == 0.0
        )
        if dangling_sum > 0.0:
            dangling_contrib = DAMPING * dangling_sum / n
            for nid in node_ids:
                new_scores[nid] = new_scores.get(nid, teleport) + dangling_contrib

        delta = sum(abs(new_scores[nid] - scores[nid]) for nid in node_ids)
        scores = new_scores
        iterations = iteration
        if delta < TOLERANCE:
            converged = True
            break

    max_score = max(scores.values()) if scores else 1.0
    if max_score > 0:
        scores = {nid: s / max_score for nid, s in scores.items()}

    return scores, iterations, converged
