"""
kg_convergence.py — Multi-seed activation and convergence scoring for the KG.

Core idea:
    Given N seed nodes (e.g. "Rahul" + "user"), run independent BFS from each.
    Nodes reached by multiple seeds are scored higher — not because they are individually
    important, but because they sit at the structural intersection of the seeds.

    This surfaces latent connections that neither RAG nor single-node KG queries find:
    e.g. "Varadarajan" is a weak neighbor of both Rahul and the user (shared PhD advisor),
    but only becomes significant once both seeds activate it independently.

    A second expansion wave from the top convergence nodes then finds further connections
    (e.g. Dave, another student of Varadarajan) that were unreachable from the original seeds.

Usage:
    from app.models.base import get_session
    from app.assistant.kg_core.kg_utils.kg_convergence import multi_seed_activation

    session = get_session()
    result = multi_seed_activation(session, seed_node_ids=["<rahul_id>", "<user_id>"])
    for node in result.convergence_nodes:
        print(node.label, node.convergence_score, node.reached_by_seeds)
    session.close()
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# How many hops to expand from each seed in the first wave.
DEFAULT_DEPTH = 2

# How many top convergence nodes to use as seeds for the second expansion wave.
DEFAULT_SECOND_WAVE_SEEDS = 5

# How many hops to expand in the second wave.
DEFAULT_SECOND_WAVE_DEPTH = 1

# Maximum nodes to *expand edges from* per seed BFS.  Visited nodes are tracked
# separately — we always record a neighbor when we first see it (so it can participate
# in convergence scoring) but we only expand its edges if the budget allows.
# This is important for high-degree seeds (e.g. the primary user node with 1000+ edges):
# we want to *visit* all their hop=1 neighbours but only *expand* the best ones at hop=2.
MAX_EXPAND_PER_SEED = 300

# Multi-seed bonus: each additional seed that reaches a node multiplies the score
# by (1 + CONVERGENCE_BONUS_PER_SEED).  Set to 0.6 → two seeds → ×1.6, three → ×2.2.
CONVERGENCE_BONUS_PER_SEED = 0.6

# Hub dampener: divide score by log(1 + degree) so highly-connected hub nodes
# (e.g. "user", "home") don't dominate the output.
HUB_DAMPENER_ENABLED = True

# Recency half-life in days for edge scoring.
RECENCY_HALF_LIFE_DAYS = 90.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScoredNode:
    node_id: str
    label: str
    node_type: str
    description: Optional[str]
    importance: float
    convergence_score: float
    reached_by_seeds: List[str]          # seed node IDs that activated this node
    reached_by_seed_labels: List[str]    # human-readable seed labels
    hop_distances: Dict[str, int]        # seed_id -> hop distance
    degree: int
    attributes: Dict[str, Any] = field(default_factory=dict)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


@dataclass
class ConvergenceResult:
    # Nodes reached by 2+ seeds, ranked by convergence_score descending.
    convergence_nodes: List[ScoredNode]
    # All activated nodes (including single-seed), ranked by convergence_score.
    all_activated_nodes: List[ScoredNode]
    # Second-wave neighbors of the top convergence nodes (may include novel nodes).
    second_wave_nodes: List[ScoredNode]
    # Seed labels used (for context).
    seed_labels: List[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recency_decay(updated_at: Optional[datetime]) -> float:
    """
    Exponential decay: 1.0 if updated now, 0.5 after RECENCY_HALF_LIFE_DAYS days.
    Falls back to 0.5 if timestamp is unavailable.
    """
    if updated_at is None:
        return 0.5
    try:
        now = datetime.now(timezone.utc)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
        return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)
    except Exception:
        return 0.5


def _edge_activation(edge: Edge) -> float:
    """
    Score for an edge as a carrier of activation.
    Uses importance × recency, defaulting to mid values when missing.

    CONVERGENCE-SPECIFIC ADJUSTMENT. This is the ONE place in the codebase
    that applies recency decay on top of edge importance. The rationale:
    the convergence engine builds a "what's contextually relevant NOW"
    briefing, so an old high-importance edge contributes less than a fresh
    one of the same raw importance. The recency multiplier ranges from 0.6
    (very stale) to 1.0 (fresh) — old edges still count substantially, but
    newer relationships pull harder.

    No other consumer applies recency decay; PageRank, card-fact admission,
    state-importance derivation, etc. all read edge.importance unmodified.
    If you find yourself wanting recency-decayed importance elsewhere,
    consider whether it belongs in the importance module proper (as a
    distinct accessor) rather than re-implementing this formula.
    """
    importance = float(edge.importance or 0.5)
    recency = _recency_decay(getattr(edge, "updated_at", None))
    return importance * (0.6 + 0.4 * recency)


def _node_degree(session: Session, node_id: str) -> int:
    """Return total degree (in + out) for hub dampening."""
    try:
        out_ = session.query(Edge).filter(Edge.source_id == node_id).count()
        in_ = session.query(Edge).filter(Edge.target_id == node_id).count()
        return out_ + in_
    except Exception as e:
        logger.debug("kg_convergence: could not get degree for %s: %s", node_id, e)
        return 1


def _bfs_from_seed(
    session: Session,
    seed_id: str,
    depth: int,
    max_nodes: int,
    seed_node_ids: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Priority-BFS from a single seed node.

    Strategy:
    - All hop=1 neighbors are always visited (recorded) regardless of budget, so that
      convergence scoring can see every direct connection.
    - From hop=1 onward, we use a max-heap ordered by activation to expand the most
      strongly-activated nodes first.  We stop expanding once `max_nodes` nodes have had
      their edges processed.  This matters for high-degree seeds (e.g. the primary user
      node with 1000+ edges): we visit all hop=1 neighbours but only expand the best
      `max_nodes` of them at hop=2.

    Returns a dict: node_id -> {
        "activation": float,   # strongest activation along any path from seed
        "hop": int,            # shortest hop distance from seed
    }

    The seed itself is included at hop 0 with activation 1.0.
    """
    visited: Dict[str, Dict[str, Any]] = {
        seed_id: {"activation": 1.0, "hop": 0}
    }
    # Max-heap: store (-activation, node_id, hop) so highest-activation pops first.
    heap: list = [(-1.0, seed_id, 0)]
    expand_count = 0

    while heap:
        neg_act, current_id, current_hop = heapq.heappop(heap)
        current_activation = -neg_act

        # Skip if we've already expanded this node (heap may have stale entries).
        # We mark expansion via a separate set.
        if current_hop >= depth:
            continue

        expand_count += 1
        # Allow all hop=0 → hop=1 (seed direct) and hop=1 → hop=2 expansions freely.
        # Only apply the budget cap from hop=2 onward (i.e., depth-3+ if depth>2).
        # The practical effect: for depth=2, *all* nodes get expanded regardless of budget.
        # For depth=3+, we cap expansion at hop=2→hop=3.
        if current_hop >= 2 and expand_count > max_nodes:
            continue

        try:
            outgoing = session.query(Edge).filter(Edge.source_id == current_id).all()
            incoming = session.query(Edge).filter(Edge.target_id == current_id).all()
        except Exception as e:
            logger.debug("kg_convergence: edge fetch failed for %s: %s", current_id, e)
            continue

        for edge in outgoing + incoming:
            neighbor_id = edge.target_id if edge.source_id == current_id else edge.source_id
            if neighbor_id == current_id:
                continue

            edge_score = _edge_activation(edge)
            propagated = current_activation * edge_score

            # --- Transparent intermediary pass-through ---
            # State and Event nodes are reified relationships (named edges).
            # We traverse through them without counting them as a hop, so that
            # Entity→Entity connections are visible at depth=1 instead of depth=2.
            # Guard: skip intermediaries that directly connect two seeds to each other
            # (e.g. "Contact" state between Jukka and Rahul) — those are relationship
            # nodes documenting the seeds' own link, not paths to third parties.
            INTERMEDIARY_MAX_DEGREE = 6
            try:
                neighbor_node = session.get(Node, neighbor_id)
                neighbor_degree = _node_degree(session, neighbor_id)
                is_intermediary = (
                    neighbor_node is not None
                    and neighbor_node.node_type in ("State", "Event")
                    and neighbor_degree <= INTERMEDIARY_MAX_DEGREE
                )
                # Disqualify if this intermediary directly bridges two seed nodes.
                if is_intermediary and seed_node_ids is not None and len(seed_node_ids) > 1:
                    inter_neighbors_out = session.query(Edge.target_id).filter(
                        Edge.source_id == neighbor_id
                    ).all()
                    inter_neighbors_in = session.query(Edge.source_id).filter(
                        Edge.target_id == neighbor_id
                    ).all()
                    inter_neighbor_ids = {r[0] for r in inter_neighbors_out + inter_neighbors_in}
                    seeds_touching = inter_neighbor_ids & set(seed_node_ids)
                    if len(seeds_touching) >= 2:
                        is_intermediary = False
            except Exception:
                is_intermediary = False

            if neighbor_id not in visited:
                visited[neighbor_id] = {"activation": propagated, "hop": current_hop + 1}
                heapq.heappush(heap, (-propagated, neighbor_id, current_hop + 1))
            else:
                if propagated > visited[neighbor_id]["activation"]:
                    visited[neighbor_id]["activation"] = propagated
                    heapq.heappush(heap, (-propagated, neighbor_id, visited[neighbor_id]["hop"]))

            # For transparent intermediaries: also enqueue all of their neighbors
            # at current_hop (not current_hop+1), so entities on the far side land
            # at the same hop distance as a direct edge.
            # Only propagate to Entity/Concept nodes — never into other State/Event
            # nodes, which would let Jukka's activity log bleed into Rahul's world.
            if is_intermediary and current_hop + 1 < depth:
                try:
                    inter_out = session.query(Edge).filter(Edge.source_id == neighbor_id).all()
                    inter_in = session.query(Edge).filter(Edge.target_id == neighbor_id).all()
                except Exception as e:
                    logger.debug("kg_convergence: intermediary edge fetch failed for %s: %s", neighbor_id, e)
                    continue

                for inter_edge in inter_out + inter_in:
                    far_id = inter_edge.target_id if inter_edge.source_id == neighbor_id else inter_edge.source_id
                    if far_id == current_id or far_id == neighbor_id:
                        continue
                    try:
                        far_node = session.get(Node, far_id)
                        if far_node is None or far_node.node_type not in ("Entity", "Concept"):
                            continue
                    except Exception:
                        continue
                    far_score = _edge_activation(inter_edge)
                    far_propagated = propagated * far_score

                    if far_id not in visited:
                        visited[far_id] = {"activation": far_propagated, "hop": current_hop + 1}
                        heapq.heappush(heap, (-far_propagated, far_id, current_hop + 1))
                    else:
                        if far_propagated > visited[far_id]["activation"]:
                            visited[far_id]["activation"] = far_propagated
                            heapq.heappush(heap, (-far_propagated, far_id, visited[far_id]["hop"]))

    return visited


def _resolve_seed_labels(session: Session, seed_node_ids: List[str]) -> Dict[str, str]:
    """Return {node_id: label} for the seed nodes."""
    labels: Dict[str, str] = {}
    for nid in seed_node_ids:
        try:
            node = session.get(Node, nid)
            labels[nid] = node.label if node else nid
        except Exception:
            labels[nid] = nid
    return labels


def _build_scored_node(
    session: Session,
    node_id: str,
    reached_by: Dict[str, Dict[str, Any]],   # seed_id -> {activation, hop}
    seed_labels: Dict[str, str],
    seed_node_ids: Set[str],
) -> Optional[ScoredNode]:
    """Build a ScoredNode from BFS results for a given node_id."""
    try:
        node = session.get(Node, node_id)
        if node is None:
            return None
    except Exception as e:
        logger.debug("kg_convergence: could not fetch node %s: %s", node_id, e)
        return None

    # Base activation: sum across all seeds that reached this node.
    base_activation = sum(info["activation"] for info in reached_by.values())

    # Multi-seed convergence bonus.
    n_seeds = len(reached_by)
    convergence_multiplier = 1.0 + CONVERGENCE_BONUS_PER_SEED * max(0, n_seeds - 1)

    raw_score = base_activation * convergence_multiplier

    # Hub dampener: penalise highly-connected nodes so generic hubs (e.g. the primary
    # user node, "home") don't dominate when acting as a seed.
    # NOTE: We only dampen when a node is reached by a SINGLE seed — if multiple seeds
    # converge on a hub, it earns its place and the dampener does not apply.
    # This is intentional: Jukka (primary user, degree=1154) reaches almost everything,
    # so single-seed activation from him is weak signal; but if Rahul also reaches the
    # same node, that convergence is genuine.
    if HUB_DAMPENER_ENABLED and node_id not in seed_node_ids and n_seeds == 1:
        degree = _node_degree(session, node_id)
        raw_score = raw_score / math.log(1.0 + max(1, degree))
    else:
        degree = _node_degree(session, node_id)

    # effective_importance applies damping (currently no-op; pass-through);
    # consumers downstream that filter by ScoredNode.importance will get
    # damped values once damping is calibrated.
    from app.assistant.importance.effective import effective_importance
    eff = effective_importance(node) if node.importance is not None else 0.5
    return ScoredNode(
        node_id=node_id,
        label=node.label,
        node_type=node.node_type or "",
        description=node.description,
        importance=float(eff),
        convergence_score=raw_score,
        reached_by_seeds=list(reached_by.keys()),
        reached_by_seed_labels=[seed_labels.get(s, s) for s in reached_by.keys()],
        hop_distances={s: info["hop"] for s, info in reached_by.items()},
        degree=degree,
        attributes=node.attributes or {},
        start_date=node.start_date,
        end_date=node.end_date,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def multi_seed_activation(
    session: Session,
    seed_node_ids: List[str],
    depth: int = DEFAULT_DEPTH,
    top_k_convergence: int = 10,
    second_wave_seeds: int = DEFAULT_SECOND_WAVE_SEEDS,
    second_wave_depth: int = DEFAULT_SECOND_WAVE_DEPTH,
    exclude_seed_nodes_from_output: bool = True,
) -> ConvergenceResult:
    """
    Run multi-seed BFS activation and return convergence-scored nodes.

    Args:
        session:                       SQLAlchemy session.
        seed_node_ids:                 Node IDs to use as activation seeds (e.g. [rahul_id, user_id]).
        depth:                         BFS hop depth per seed.
        top_k_convergence:             How many top convergence nodes to return.
        second_wave_seeds:             How many top convergence nodes to use as seeds for wave 2.
        second_wave_depth:             BFS depth for the second expansion wave.
        exclude_seed_nodes_from_output: Don't return the seeds themselves in the results.

    Returns:
        ConvergenceResult with convergence_nodes, all_activated_nodes, second_wave_nodes.
    """
    if not seed_node_ids:
        logger.warning("kg_convergence: multi_seed_activation called with no seeds")
        return ConvergenceResult(
            convergence_nodes=[],
            all_activated_nodes=[],
            second_wave_nodes=[],
            seed_labels=[],
        )

    seed_set = set(seed_node_ids)
    seed_labels = _resolve_seed_labels(session, seed_node_ids)

    logger.info(
        "kg_convergence: starting multi-seed activation seeds=%s depth=%d",
        [seed_labels.get(s, s) for s in seed_node_ids],
        depth,
    )

    # --- Wave 1: independent BFS from each seed ---
    # per_seed[seed_id][node_id] = {activation, hop}
    per_seed: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for seed_id in seed_node_ids:
        per_seed[seed_id] = _bfs_from_seed(session, seed_id, depth, MAX_EXPAND_PER_SEED, seed_node_ids=seed_node_ids)
        logger.info(
            "kg_convergence: seed '%s' activated %d nodes",
            seed_labels.get(seed_id, seed_id),
            len(per_seed[seed_id]),
        )

    # --- Merge: for each node, collect which seeds reached it and their activation ---
    # node_id -> {seed_id: {activation, hop}}
    merged: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for seed_id, bfs_result in per_seed.items():
        for node_id, info in bfs_result.items():
            merged[node_id][seed_id] = info

    # --- Build ScoredNode for every activated node ---
    all_scored: List[ScoredNode] = []
    for node_id, reached_by in merged.items():
        if exclude_seed_nodes_from_output and node_id in seed_set:
            continue
        scored = _build_scored_node(session, node_id, reached_by, seed_labels, seed_set)
        if scored is not None:
            all_scored.append(scored)

    all_scored.sort(key=lambda n: n.convergence_score, reverse=True)

    # Split into convergence nodes (2+ seeds) vs single-seed.
    convergence_nodes = [n for n in all_scored if len(n.reached_by_seeds) >= 2]
    convergence_nodes = convergence_nodes[:top_k_convergence]

    logger.info(
        "kg_convergence: wave 1 complete — total_activated=%d convergence_nodes=%d",
        len(all_scored),
        len(convergence_nodes),
    )
    for n in convergence_nodes:
        logger.info(
            "  convergence: '%s' (%s) score=%.3f seeds=%s hops=%s",
            n.label,
            n.node_type,
            n.convergence_score,
            n.reached_by_seed_labels,
            n.hop_distances,
        )

    # --- Wave 2: expand from top convergence nodes ---
    # Find nodes like Dave that are 1 hop from Varadarajan.
    # We exclude nodes that were themselves convergence nodes, but NOT all wave-1
    # visited nodes — otherwise everything Jukka's BFS touched is excluded and
    # the second wave finds nothing.
    second_wave_nodes: List[ScoredNode] = []
    if convergence_nodes and second_wave_seeds > 0:
        # Use ALL convergence nodes as wave-2 seeds (not just top-N) so that
        # lower-ranked but interesting nodes like Varadarajan also get expanded.
        wave2_seeds = [n.node_id for n in convergence_nodes]
        # Only exclude the convergence nodes themselves and the original seeds.
        already_converged = {n.node_id for n in convergence_nodes} | seed_set

        wave2_per_seed: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for seed_id in wave2_seeds:
            wave2_per_seed[seed_id] = _bfs_from_seed(
                session, seed_id, second_wave_depth, MAX_EXPAND_PER_SEED, seed_node_ids=seed_node_ids
            )

        wave2_merged: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        for seed_id, bfs_result in wave2_per_seed.items():
            for node_id, info in bfs_result.items():
                if node_id not in already_converged:
                    wave2_merged[node_id][seed_id] = info

        for node_id, reached_by in wave2_merged.items():
            scored = _build_scored_node(session, node_id, reached_by, seed_labels, seed_set)
            if scored is not None:
                second_wave_nodes.append(scored)

        second_wave_nodes.sort(key=lambda n: n.convergence_score, reverse=True)

        logger.info(
            "kg_convergence: wave 2 complete — novel_nodes=%d",
            len(second_wave_nodes),
        )

    return ConvergenceResult(
        convergence_nodes=convergence_nodes,
        all_activated_nodes=all_scored,
        second_wave_nodes=second_wave_nodes,
        seed_labels=[seed_labels.get(s, s) for s in seed_node_ids],
    )


def resolve_entity_to_node_id(session: Session, label: str) -> Optional[str]:
    """
    Best-effort resolution of a text label to a KG node ID.

    Tries exact match first, then case-insensitive, then alias match.
    On ties, prefers the node with the highest degree (most connected).
    Returns None if not found.
    """
    try:
        # Exact match
        candidates = session.query(Node).filter(Node.label == label).all()
        if not candidates:
            candidates = session.query(Node).filter(
                Node.label.ilike(label)
            ).all()
        if not candidates:
            # Try aliases (JSON array stored as text)
            all_nodes = session.query(Node).filter(
                Node.aliases.isnot(None)
            ).all()
            candidates = [
                n for n in all_nodes
                if isinstance(n.aliases, list)
                and any(str(a).lower() == label.lower() for a in n.aliases)
            ]

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].id

        # Tiebreak by degree
        best = max(candidates, key=lambda n: _node_degree(session, n.id))
        return best.id
    except Exception as e:
        logger.debug("kg_convergence: resolve_entity_to_node_id failed for %r: %s", label, e)
        return None
