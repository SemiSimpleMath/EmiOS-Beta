"""Personalized PageRank for the lens.

The lens computes a bounded subgraph by:
  1. Optionally filter the KG to edges valid in a time window.
  2. Build an in-memory NetworkX graph of nodes + edges.
  3. Run PageRank with `personalization` set on the seed nodes.
  4. Return the top-K nodes by score (with their inter-edges).

Cached by (sorted seeds tuple, time-mode, time-window) so rapid expansion
clicks don't rebuild the graph each time. Cache TTL is short — graph mutates
out from under us via the audited mutation pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx
from sqlalchemy import or_

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.models.db_manager import get_db_manager
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# State-shaped node types — these get visually subdued in the lens. The
# pagerank computation treats them like any other node, but the API caller
# can choose to push them down with a multiplier so entities dominate the
# top-K cut.
STATE_NODE_TYPES = frozenset({"State", "Goal"})

# Default cap for visible nodes per query.
DEFAULT_LIMIT = 50
HARD_LIMIT = 100

# Cache: (sorted seeds tuple, time_mode, time_from, time_to) → (timestamp, result)
_CACHE: Dict[Tuple, Tuple[float, "SeedGraphResult"]] = {}
_CACHE_TTL_SECONDS = 30


@dataclass
class SeedGraphResult:
    nodes: List[Dict]
    edges: List[Dict]
    pagerank: Dict[str, float]
    seeds: List[str]
    time_mode: str
    time_from: Optional[str]
    time_to: Optional[str]
    total_candidates: int


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _edge_active_in_window(
    edge: Edge,
    time_mode: str,
    time_from: Optional[date],
    time_to: Optional[date],
) -> bool:
    """Decide if `edge` should be visible under the active time filter.

    Edges don't always carry their own validity dates — but the State
    nodes they connect through do. For v0, we treat an edge as "active"
    if either endpoint's validity overlaps the requested window. The
    bookkeeping of which endpoint owns the validity is upstream.
    """
    if time_mode == "lifetime":
        return True
    # Currently true: rely on the connected nodes' validity (handled by
    # node filter); edges always pass.
    if time_mode == "current":
        return True
    if time_mode == "range":
        # Only edges connected to nodes in-window survive — caller filters
        # nodes first; we just trust the node filter.
        return True
    return True


def _node_active_in_window(
    node: Node,
    time_mode: str,
    time_from: Optional[date],
    time_to: Optional[date],
) -> bool:
    if time_mode == "lifetime":
        return True

    start = _parse_iso_date(node.start_date.isoformat() if node.start_date else None)
    end = _parse_iso_date(node.end_date.isoformat() if node.end_date else None)

    if time_mode == "current":
        # Currently true: node has no end_date OR end_date is in the future.
        if end is None:
            return True
        return end >= date.today()

    if time_mode == "range":
        if time_from is None and time_to is None:
            return True
        # Node window: [start, end]. Window: [time_from, time_to].
        # Overlap when not (node ends before window starts OR node starts after window ends).
        if time_from and end and end < time_from:
            return False
        if time_to and start and start > time_to:
            return False
        return True

    return True


def _pick_anchors_around_seed(
    seed_id: str,
    g: "nx.Graph",
    node_by_id: Dict[str, "Node"],
    *,
    k: int = 6,
) -> List[str]:
    """Promote the seed's top-K first-degree Entity neighbors to anchors.

    Anchors become layout cluster centers — each gets its own district on
    the canvas. Without this, a single user-seed produces a mono-cluster
    "epicenter with everything orbiting" view, which is the wrong mental
    model. With auto-anchors, the seed's most-important people become
    co-equal districts in their own right.
    """
    if not g.has_node(seed_id):
        return []
    candidates: List[tuple] = []
    for nb in g.neighbors(seed_id):
        n = node_by_id.get(nb)
        if n is None:
            continue
        # Anchor only Entity-class nodes — not states, goals, events. Those
        # are intermediating relationships, not co-equal districts.
        if (n.node_type or "") != "Entity":
            continue
        edge_data = g.get_edge_data(seed_id, nb) or {}
        weight = float(edge_data.get("weight", 1.0))
        importance = float(getattr(n, "importance", None) or 0.5)
        # Score: edge weight to seed + node importance. Both signal "matters
        # to the user" in different ways.
        candidates.append((weight + importance, nb))
    candidates.sort(reverse=True)
    return [nb for _, nb in candidates[:k]]


def compute_seed_graph(
    *,
    seed_ids: List[str],
    limit: int = DEFAULT_LIMIT,
    time_mode: str = "current",
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    state_score_multiplier: float = 0.4,
    include_concepts: bool = False,
    auto_anchors_per_seed: int = 6,
) -> SeedGraphResult:
    """Compute the bounded subgraph for the lens.

    Args:
      seed_ids: nodes to seed the personalized PageRank with. Empty seeds
        fall back to a uniform PageRank (rarely useful — caller should
        always provide at least the user's node).
      limit: soft cap on returned node count (top-K by score). Clamped to
        HARD_LIMIT.
      time_mode: "current" | "lifetime" | "range".
      time_from / time_to: ISO date strings; only honored when
        time_mode == "range".
      state_score_multiplier: applied to the score of State/Goal nodes
        before top-K selection so entities dominate the cut. 1.0 disables
        the bias.

    Returns: SeedGraphResult with nodes, edges (between visible nodes),
    pagerank scores, and metadata.
    """
    seed_ids = [s for s in (seed_ids or []) if isinstance(s, str) and s.strip()]
    limit = max(1, min(int(limit or DEFAULT_LIMIT), HARD_LIMIT))
    # When auto-anchoring, bump effective limit so each district has room
    # for a real neighborhood. Each anchor needs ~3-5 cluster items to feel
    # populated; with k=6 anchors per seed plus the seed itself, we need
    # at least ~50 + (6 × 4) = ~75 items.
    if auto_anchors_per_seed > 0 and len(seed_ids) <= 2 and limit < 80:
        limit = min(80, HARD_LIMIT)

    cache_key = (
        tuple(sorted(seed_ids)),
        time_mode,
        time_from or "",
        time_to or "",
        limit,
        state_score_multiplier,
        include_concepts,
        auto_anchors_per_seed,
    )
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        logger.debug("compute_seed_graph cache hit: %s", cache_key)
        return cached[1]

    tf = _parse_iso_date(time_from)
    tt = _parse_iso_date(time_to)

    with get_db_manager().read_session() as session:
        all_nodes = session.query(Node).all()
        seed_set_for_filter = set(seed_ids)
        node_by_id: Dict[str, Node] = {}
        for n in all_nodes:
            if not _node_active_in_window(n, time_mode, tf, tt):
                continue
            # Concept nodes are pure taxonomy scaffolding ("animal",
            # "career", "city") — visual noise in the personal lens.
            # Excluded by default; opt-in via include_concepts when the
            # user explicitly asks. Seeds always survive the filter.
            if (
                not include_concepts
                and (n.node_type or "") == "Concept"
                and str(n.id) not in seed_set_for_filter
            ):
                continue
            node_by_id[str(n.id)] = n

        all_edges = session.query(Edge).all()
        active_edges: List[Edge] = []
        for e in all_edges:
            sid = str(e.source_id)
            tid = str(e.target_id)
            if sid not in node_by_id or tid not in node_by_id:
                continue
            if not _edge_active_in_window(e, time_mode, tf, tt):
                continue
            active_edges.append(e)

        # Build NetworkX graph (undirected for PageRank symmetry — the lens
        # treats relationships as bidirectional for relevance purposes).
        g = nx.Graph()
        for nid, n in node_by_id.items():
            g.add_node(nid, node_type=str(n.node_type or ""))
        for e in active_edges:
            sid = str(e.source_id)
            tid = str(e.target_id)
            # Combine importance + confidence as edge weight; default 1.0.
            try:
                weight = float(e.importance or 0.5) + float(e.confidence or 0.5)
            except (TypeError, ValueError):
                weight = 1.0
            g.add_edge(sid, tid, weight=max(0.1, weight))

        if g.number_of_nodes() == 0:
            empty = SeedGraphResult([], [], {}, seed_ids, time_mode, time_from, time_to, 0)
            _CACHE[cache_key] = (now, empty)
            return empty

        # Filter seeds to those that survived the time filter.
        valid_seeds = [s for s in seed_ids if s in node_by_id]
        personalization: Optional[Dict[str, float]] = None
        if valid_seeds:
            personalization = {nid: 0.0 for nid in g.nodes}
            for s in valid_seeds:
                personalization[s] = 1.0 / len(valid_seeds)

        # Personalized PageRank.
        try:
            scores = nx.pagerank(
                g,
                alpha=0.85,
                personalization=personalization,
                weight="weight",
                max_iter=200,
                tol=1.0e-6,
            )
        except (nx.PowerIterationFailedConvergence, nx.NetworkXError) as e:
            logger.warning("pagerank did not converge: %s — falling back to uniform", e)
            scores = nx.pagerank(g, alpha=0.85, weight="weight")

        # Apply state-score-multiplier bias so entities dominate the cut,
        # but seeds are always preserved (they're what the user picked).
        biased: Dict[str, float] = {}
        seed_set: Set[str] = set(valid_seeds)
        for nid, score in scores.items():
            if nid in seed_set:
                biased[nid] = score
                continue
            n = node_by_id.get(nid)
            if n is not None and (n.node_type or "") in STATE_NODE_TYPES:
                biased[nid] = score * state_score_multiplier
            else:
                biased[nid] = score

        # Two-phase top-K selection.
        # Phase 1: entities (and seeds) fill ~80% of the cap by personalized PR.
        #          State/Goal nodes are skipped here — they fill phase 2 only
        #          if they bridge selected entities.
        # Phase 2: add State/Goal nodes that connect ≥2 already-visible nodes
        #          (bridge nodes). Without these, visible entities have no
        #          connecting edges in the rendered subgraph.
        ranked = sorted(biased.items(), key=lambda kv: (-kv[1], kv[0]))
        visible: Set[str] = set(valid_seeds)
        entity_cap = max(1, int(limit * 0.8))

        for nid, _score in ranked:
            if len(visible) >= entity_cap:
                break
            n = node_by_id.get(nid)
            if n is None:
                continue
            if (n.node_type or "") in STATE_NODE_TYPES:
                continue
            visible.add(nid)

        # Phase 2: add bridge states/goals (still ranked highest first).
        for nid, _score in ranked:
            if len(visible) >= limit:
                break
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None or (n.node_type or "") not in STATE_NODE_TYPES:
                continue
            # Count visible neighbors — only admit if it bridges ≥2.
            try:
                neighbors = list(g.neighbors(nid))
            except nx.NetworkXError:
                continue
            visible_neighbors = sum(1 for nb in neighbors if nb in visible)
            if visible_neighbors >= 2:
                visible.add(nid)

        # Phase 3: if we still have room and there are non-bridge state/goal
        # nodes that touch a seed directly, admit them so seeds aren't
        # represented as disconnected dots.
        for nid, _score in ranked:
            if len(visible) >= limit:
                break
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None:
                continue
            try:
                neighbors = set(g.neighbors(nid))
            except nx.NetworkXError:
                continue
            if seed_set & neighbors:
                visible.add(nid)

        # Auto-anchor promotion. Each seed's top-K first-degree Entity
        # neighbors become co-equal "districts" on the canvas. This breaks
        # the mono-cluster "epicenter" view: instead of everything orbiting
        # one node, the major people get their own neighborhoods.
        anchor_set: Set[str] = set(valid_seeds)
        if auto_anchors_per_seed > 0:
            for sid in valid_seeds:
                for anchor_id in _pick_anchors_around_seed(
                    sid, g, node_by_id, k=auto_anchors_per_seed,
                ):
                    anchor_set.add(anchor_id)
                    visible.add(anchor_id)  # guarantee anchors are visible

        # Inter-visible edges only.
        edges_out: List[Dict] = []
        seen_edge_ids: Set[str] = set()
        for e in active_edges:
            sid = str(e.source_id)
            tid = str(e.target_id)
            if sid not in visible or tid not in visible:
                continue
            eid = str(e.id)
            if eid in seen_edge_ids:
                continue
            seen_edge_ids.add(eid)
            edges_out.append({
                "id": eid,
                "source_id": sid,
                "target_id": tid,
                "relationship_type": str(e.relationship_type or ""),
                "sentence": e.sentence or "",
                "importance": float(e.importance or 0.5),
                "confidence": float(e.confidence or 0.5),
            })

        # Assign each visible node a `primary_anchor_id` — the anchor it
        # should cluster under in the layout. Anchors are seeds plus the
        # auto-promoted top-K first-degree neighbors per seed. For an anchor
        # itself, primary = self. For non-anchors: shortest path to the
        # nearest anchor in the visible subgraph.
        visible_subgraph = g.subgraph(visible).copy()
        primary_by_node: Dict[str, str] = {}
        anchor_list_for_fallback = list(anchor_set) or list(valid_seeds) or [next(iter(visible))]

        for nid in visible:
            if nid in anchor_set:
                primary_by_node[nid] = nid
                continue
            best_seed = None
            best_dist = float("inf")
            for sid in anchor_set:
                if not visible_subgraph.has_node(sid):
                    continue
                try:
                    d = nx.shortest_path_length(visible_subgraph, source=sid, target=nid)
                    if d < best_dist:
                        best_dist = d
                        best_seed = sid
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
            primary_by_node[nid] = best_seed or anchor_list_for_fallback[0]

        # Pull global layout positions. Lazy-computed on first call. Each
        # node has a stable (x, y) regardless of which query brought it in.
        from app.me.layout import get_layout
        layout = get_layout()

        nodes_out: List[Dict] = []
        for nid in visible:
            n = node_by_id[nid]
            pos = layout.get(nid, (0.0, 0.0))
            nodes_out.append({
                "id": nid,
                "label": n.label or "",
                "node_type": str(n.node_type or ""),
                "category": n.category,
                "aliases": list(n.aliases or []),
                "description": n.description or "",
                "start_date": n.start_date.isoformat() if n.start_date else None,
                "end_date": n.end_date.isoformat() if n.end_date else None,
                "importance": float(n.importance or 0.5),
                "pagerank_score": float(scores.get(nid, 0.0)),
                "is_seed": nid in seed_set,
                "is_anchor": nid in anchor_set,
                "primary_anchor_id": primary_by_node.get(nid),
                "x": float(pos[0]),
                "y": float(pos[1]),
            })

        result = SeedGraphResult(
            nodes=nodes_out,
            edges=edges_out,
            pagerank={n["id"]: n["pagerank_score"] for n in nodes_out},
            seeds=valid_seeds,
            time_mode=time_mode,
            time_from=time_from,
            time_to=time_to,
            total_candidates=len(node_by_id),
        )
        _CACHE[cache_key] = (now, result)

        logger.info(
            "compute_seed_graph: seeds=%s mode=%s window=%s..%s → %d/%d nodes, %d edges",
            valid_seeds, time_mode, time_from, time_to,
            len(nodes_out), len(node_by_id), len(edges_out),
        )
        return result


def invalidate_cache() -> None:
    """Drop the cache. Call after any KG mutation."""
    _CACHE.clear()
