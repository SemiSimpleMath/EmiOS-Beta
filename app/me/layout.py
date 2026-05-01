"""Global stable layout for the personal lens.

Every KG node gets a fixed (x, y) on a giant 2D map. The lens viewport
shows whatever 50 nodes are visible for the current query, but each node
sits at its canonical map position so identity is spatial — Katy is
always in the same place on the map regardless of which query brought her
into view.

Strategy:
  1. Build the full KG graph (time-filter-free; the layout is universal).
  2. Detect communities (greedy modularity).
  3. Place each community center on a Vogel-spiral with large radius
     between centers — so districts don't visually crowd each other.
  4. Within each community, run a local force-directed layout to spread
     members. Translate by the community center.
  5. Persist to data/me_layout.json keyed by node id.

Layout is computed lazily on first request and reused thereafter. To
force a recompute (e.g., after large KG mutation), delete the JSON file
or call ``regenerate_layout()``.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

LAYOUT_DIR = Path(__file__).resolve().parents[2] / "data"

# Default cache: full unfiltered KG layout.
LAYOUT_PATH = LAYOUT_DIR / "me_layout.json"


def _layout_path_for(categories: Optional[List[str]] = None) -> Path:
    """Per-filter layout file. Persons-only is the most useful mode today."""
    if not categories:
        return LAYOUT_PATH
    cleaned = sorted({c.lower().strip() for c in categories if c})
    if not cleaned:
        return LAYOUT_PATH
    suffix = "+".join(cleaned)
    return LAYOUT_DIR / f"me_layout.{suffix}.json"

# Tuned for ~5,000 nodes on a multi-megapixel canvas.
COMMUNITY_BASE_RADIUS = 1500.0   # how far the first community center sits
COMMUNITY_SPIRAL_FACTOR = 1.4    # how aggressively centers spiral outward
COMMUNITY_LOCAL_SCALE = 1100.0   # spread of nodes within a community
SINGLETON_RADIUS = 600.0         # singleton communities scatter on a small ring
GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))

_LAYOUT_CACHE: Dict[str, Dict[str, Tuple[float, float]]] = {}


def _empty_position() -> Tuple[float, float]:
    return (0.0, 0.0)


def _cache_key(categories: Optional[List[str]]) -> str:
    if not categories:
        return ""
    return "+".join(sorted({c.lower().strip() for c in categories if c}))


def get_layout(
    categories: Optional[List[str]] = None,
) -> Dict[str, Tuple[float, float]]:
    """Return the cached layout, loading from disk on first call.

    Computes from scratch if the file is missing. Per-filter cache.
    """
    key = _cache_key(categories)
    cached = _LAYOUT_CACHE.get(key)
    if cached is not None:
        return cached
    path = _layout_path_for(categories)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            parsed = {
                str(k): (float(v[0]), float(v[1]))
                for k, v in data.items()
                if isinstance(v, (list, tuple)) and len(v) == 2
            }
            _LAYOUT_CACHE[key] = parsed
            logger.info("me layout: loaded %d positions from %s", len(parsed), path)
            return parsed
        except Exception as e:
            logger.warning("me layout: failed to load %s, will recompute: %s", path, e)
    # Lazy compute on first call.
    fresh = regenerate_layout(categories=categories)
    _LAYOUT_CACHE[key] = fresh
    return fresh


def invalidate(categories: Optional[List[str]] = None) -> None:
    """Force the next get_layout() to recompute."""
    if categories is None:
        _LAYOUT_CACHE.clear()
    else:
        _LAYOUT_CACHE.pop(_cache_key(categories), None)


def regenerate_layout(
    categories: Optional[List[str]] = None,
) -> Dict[str, Tuple[float, float]]:
    """Recompute the global layout and persist to disk."""
    started = time.time()
    g = _build_full_graph(categories=categories)
    positions = _layout_graph(g)
    _save_layout(positions, categories=categories)
    elapsed = time.time() - started
    logger.info(
        "me layout: regenerated %d positions in %.1fs (categories=%s)",
        len(positions), elapsed, categories,
    )
    return positions


def _build_full_graph(
    categories: Optional[List[str]] = None,
) -> nx.Graph:
    """Build a NetworkX graph from the KG, optionally filtered by category.

    When categories is provided, only Entity-type nodes whose category is in
    the filter survive. Edges between surviving nodes are kept; edges to
    filtered-out nodes are dropped.
    """
    cat_filter: Optional[Set[str]] = None
    if categories:
        cat_filter = {c.lower().strip() for c in categories if c}

    g = nx.Graph()
    with get_db_manager().read_session() as session:
        for n in session.query(Node).all():
            if cat_filter is not None:
                if (n.node_type or "") != "Entity":
                    continue
                if (n.category or "").lower().strip() not in cat_filter:
                    continue
            g.add_node(str(n.id), node_type=str(n.node_type or ""))
        for e in session.query(Edge).all():
            sid = str(e.source_id)
            tid = str(e.target_id)
            if not g.has_node(sid) or not g.has_node(tid):
                continue
            try:
                weight = float(e.importance or 0.5) + float(e.confidence or 0.5)
            except (TypeError, ValueError):
                weight = 1.0
            g.add_edge(sid, tid, weight=max(0.1, weight))
    return g


def _layout_graph(g: nx.Graph) -> Dict[str, Tuple[float, float]]:
    """Compute (x, y) for every node in g via community-driven layout."""
    positions: Dict[str, Tuple[float, float]] = {}
    if g.number_of_nodes() == 0:
        return positions

    # Community detection. greedy_modularity_communities returns a list of
    # frozensets sorted by size, descending. For a typical personal KG this
    # gives ~10-30 distinguishable communities.
    try:
        from networkx.algorithms.community import greedy_modularity_communities
        communities = list(greedy_modularity_communities(g))
    except Exception as e:
        logger.warning("community detection failed (%s); using single-pass spring", e)
        # Fallback: single big spring layout.
        try:
            local = nx.spring_layout(g, k=2.5, iterations=120, seed=42)
            return {nid: (float(p[0]) * 4000, float(p[1]) * 4000) for nid, p in local.items()}
        except Exception as e2:
            logger.error("fallback spring_layout also failed: %s", e2)
            return {nid: _empty_position() for nid in g.nodes}

    # Sort communities largest-first so the biggest district sits closest to
    # the origin (where the user expects to land). Vogel spiral handles the
    # rest — communities tile outward in a sunflower-seed pattern.
    communities = [list(c) for c in communities]
    communities.sort(key=lambda c: -len(c))

    isolated_nodes = []

    for i, members in enumerate(communities):
        if len(members) == 0:
            continue

        # Community center on Vogel spiral.
        r = COMMUNITY_BASE_RADIUS * math.sqrt(i + 1) * COMMUNITY_SPIRAL_FACTOR
        theta = i * GOLDEN_ANGLE
        cx = r * math.cos(theta)
        cy = r * math.sin(theta)

        if len(members) == 1:
            # Singleton — place at community center directly.
            positions[members[0]] = (cx, cy)
            continue

        sub = g.subgraph(members)
        try:
            local = nx.spring_layout(
                sub, k=2.5, iterations=80, seed=42 + i,
            )
        except Exception as e:
            logger.warning("local spring_layout failed for community %d: %s", i, e)
            # Fallback: small ring.
            n_members = len(members)
            local = {}
            for j, nid in enumerate(members):
                ang = (2 * math.pi * j) / n_members
                local[nid] = (math.cos(ang), math.sin(ang))

        # Translate local positions to community center, scaled.
        # Larger communities get bigger local scale (more breathing room).
        size_factor = 1.0 + math.log10(max(2, len(members)))
        scale = COMMUNITY_LOCAL_SCALE * size_factor
        for nid, (lx, ly) in local.items():
            positions[nid] = (cx + float(lx) * scale, cy + float(ly) * scale)

    # Catch any nodes that somehow weren't placed (shouldn't happen, but
    # belt-and-suspenders).
    placed = set(positions.keys())
    n_total = g.number_of_nodes()
    if len(placed) < n_total:
        unplaced = [n for n in g.nodes if n not in placed]
        logger.warning("me layout: %d unplaced nodes — scattering", len(unplaced))
        for j, nid in enumerate(unplaced):
            ang = j * GOLDEN_ANGLE
            r = SINGLETON_RADIUS + j * 30
            positions[nid] = (r * math.cos(ang), r * math.sin(ang))

    return positions


def _save_layout(
    positions: Dict[str, Tuple[float, float]],
    *,
    categories: Optional[List[str]] = None,
) -> None:
    path = _layout_path_for(categories)
    path.parent.mkdir(parents=True, exist_ok=True)
    serial = {k: [v[0], v[1]] for k, v in positions.items()}
    path.write_text(json.dumps(serial), encoding="utf-8")


def position_for(
    node_id: str,
    *,
    categories: Optional[List[str]] = None,
) -> Tuple[float, float]:
    """Look up a node's position, returning (0, 0) if unknown."""
    return get_layout(categories).get(node_id, _empty_position())
