"""Global stable layout for the personal lens.

Every KG node gets a fixed (x, y) on a giant 2D map. The lens viewport
shows whatever 50 nodes are visible for the current query, but each node
sits at its canonical map position so identity is spatial — Katy is
always in the same place on the map regardless of which query brought her
into view.

Strategy (anchor-rooted, not community-rooted):
  1. Pick top-N important persons as anchors (importance-weighted).
  2. Place anchors on a Vogel spiral centered at origin, most important
     closest to (0, 0). The seed user sits at the very center.
  3. For each non-anchor node, find its **primary anchor** = the anchor
     it's most strongly connected to (1-hop edge weight, or best 2-hop
     product if no direct edge to any anchor).
  4. Cluster each anchor's primary-set around that anchor via local
     spring layout pinned at the anchor.
  5. Persist to data/me_layout.json keyed by node id.

Why this beats community detection: Nicole-the-friend-of-Katy may have
only one or two edges, all of them to Katy's State nodes. Modularity
clustering can't tell that Nicole "belongs near Katy" — it just sees a
sparsely-connected node and places her wherever the spring forces drift
her. Anchor-rooting honors the user's mental model: people sit near
whoever they're closest to.

Layout is computed lazily on first request and reused thereafter. To
force a recompute (e.g., after large KG mutation), delete the JSON file
or call ``regenerate_layout()``.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

LAYOUT_DIR = Path(__file__).resolve().parents[2] / "data"
LAYOUT_PATH = LAYOUT_DIR / "me_layout.json"


def _layout_path_for(categories: Optional[List[str]] = None) -> Path:
    if not categories:
        return LAYOUT_PATH
    cleaned = sorted({c.lower().strip() for c in categories if c})
    if not cleaned:
        return LAYOUT_PATH
    suffix = "+".join(cleaned)
    return LAYOUT_DIR / f"me_layout.{suffix}.json"


# Anchor-rooted layout tuning.
N_ANCHORS = 15                   # how many top persons get their own neighborhood
ANCHOR_RADIUS_PER_MASS = 220.0   # spiral radius = this × sqrt(cumulative cluster mass)
INTER_ANCHOR_PADDING = 1.4       # multiplier on each anchor's footprint when packing
ORPHAN_RING_RADIUS = 12000.0     # nodes that can't reach any anchor scatter on this ring
GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))

# Per-type bbox in graph-coordinate units. Used by the deterministic
# Vogel spiral to compute radial spacing that guarantees no overlaps
# at "fully revealed" zoom — bigger boxes need more clearance.
BBOX_BY_TYPE: Dict[str, Tuple[float, float]] = {
    "Entity": (220.0, 60.0),
    "State":  (110.0, 36.0),
    "Event":  (140.0, 40.0),
    "Goal":   (110.0, 36.0),
    "Concept": (90.0, 30.0),
}
BBOX_DEFAULT = (160.0, 50.0)
COLLISION_PADDING = 24.0


def _bbox_for(node_type: str) -> Tuple[float, float]:
    return BBOX_BY_TYPE.get(node_type or "", BBOX_DEFAULT)


def _bbox_radius(node_type: str) -> float:
    """Half the bbox diagonal — a conservative collision radius for any
    rotation. Used as the per-node footprint in radial placement."""
    w, h = _bbox_for(node_type)
    return math.hypot(w, h) / 2.0


def _vogel_place_cluster(
    members: List[str],
    importance: Dict[str, float],
    node_type_of: Dict[str, str],
    anchor_id: str,
    anchor_pos: Tuple[float, float],
) -> Dict[str, Tuple[float, float]]:
    """Deterministic Vogel spiral placement for a single cluster.

    Members are sorted by importance ASCENDING — least-important members
    orbit closest to the anchor.

    The reason (per user): you only see unimportant nodes by zooming
    into their region. If unimportant nodes lived at the periphery,
    zooming in toward the anchor would push them off-screen and they'd
    never be reachable. Putting them close to the anchor means
    zoom-into-anchor naturally brings them into view as their on-screen
    claims shrink and stop colliding with the anchor.

    Important nodes can sit far — priority-collision keeps them visible
    at overview zoom regardless of their distance from the anchor.

    Anchor sits at center.
    """
    positions: Dict[str, Tuple[float, float]] = {anchor_id: anchor_pos}
    others = [m for m in members if m != anchor_id]
    if not others:
        return positions

    # Sort by importance ascending (low first → close orbit → in viewport
    # when user zooms in to discover them). Ties broken by id for determinism.
    others.sort(
        key=lambda nid: (float(importance.get(nid, 5.0)), nid),
    )

    ax, ay = anchor_pos
    # The anchor occupies a footprint too — start the cumulative area
    # tally with it so member 0 sits clear of the anchor's box.
    anchor_radius = _bbox_radius(node_type_of.get(anchor_id, "Entity"))
    cumulative_area = math.pi * (anchor_radius + COLLISION_PADDING) ** 2

    for i, nid in enumerate(others, start=1):
        nt = node_type_of.get(nid, "Entity")
        node_radius = _bbox_radius(nt)
        # Each node's "claim" is a circle of radius node_radius + padding.
        node_area = math.pi * (node_radius + COLLISION_PADDING) ** 2
        # Cumulative area before this node defines the current spiral
        # radius — sqrt(cumulative_area / π) gives the radius enclosing
        # all prior claims.
        cumulative_area += node_area / 2.0
        r = math.sqrt(cumulative_area / math.pi)
        theta = i * GOLDEN_ANGLE
        positions[nid] = (ax + r * math.cos(theta), ay + r * math.sin(theta))
        cumulative_area += node_area / 2.0
    return positions

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
    fresh = regenerate_layout(categories=categories)
    _LAYOUT_CACHE[key] = fresh
    return fresh


def invalidate(categories: Optional[List[str]] = None) -> None:
    if categories is None:
        _LAYOUT_CACHE.clear()
    else:
        _LAYOUT_CACHE.pop(_cache_key(categories), None)


def regenerate_layout(
    categories: Optional[List[str]] = None,
) -> Dict[str, Tuple[float, float]]:
    started = time.time()
    g, importance, seed_id = _build_full_graph(categories=categories)
    positions = _layout_anchor_rooted(g, importance, seed_id)
    _save_layout(positions, categories=categories)
    elapsed = time.time() - started
    logger.info(
        "me layout: regenerated %d positions in %.1fs (categories=%s)",
        len(positions), elapsed, categories,
    )
    return positions


def _build_full_graph(
    categories: Optional[List[str]] = None,
) -> Tuple[nx.Graph, Dict[str, float], Optional[str]]:
    """Returns (graph, importance_map, seed_user_node_id)."""
    from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE
    importance = dict(get_importance_map())

    cat_filter: Optional[Set[str]] = None
    if categories:
        cat_filter = {c.lower().strip() for c in categories if c}

    g = nx.Graph()
    seed_id: Optional[str] = None

    with get_db_manager().read_session() as session:
        # Try to identify the user's own node — it sits at origin.
        from app.assistant.utils.identity_names import get_required_primary_user_name
        user_first = get_required_primary_user_name()
        seed_node = (
            session.query(Node)
            .filter(Node.label == user_first, Node.node_type == "Entity")
            .first()
        )
        if seed_node is not None:
            seed_id = str(seed_node.id)

        for n in session.query(Node).all():
            if cat_filter is not None:
                if (n.node_type or "") != "Entity":
                    continue
                if (n.category or "").lower().strip() not in cat_filter:
                    continue
            nid = str(n.id)
            g.add_node(
                nid,
                node_type=str(n.node_type or ""),
                category=str(n.category or ""),
                label=str(n.label or ""),
            )
            importance.setdefault(nid, DEFAULT_SCORE)
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
    return g, importance, seed_id


def _pick_anchors(
    g: nx.Graph,
    importance: Dict[str, float],
    seed_id: Optional[str],
    n_anchors: int,
) -> List[str]:
    """Top-N person-Entity nodes by importance, with seed always first."""
    candidates: List[Tuple[float, str]] = []
    for nid, attrs in g.nodes(data=True):
        if attrs.get("node_type") != "Entity":
            continue
        if attrs.get("category") != "person":
            continue
        candidates.append((importance.get(nid, 5.0), nid))
    candidates.sort(reverse=True)
    picked: List[str] = []
    if seed_id and g.has_node(seed_id):
        picked.append(seed_id)
    for _, nid in candidates:
        if nid in picked:
            continue
        picked.append(nid)
        if len(picked) >= n_anchors:
            break
    return picked


def _primary_anchor_map(
    g: nx.Graph,
    anchors: List[str],
    seed_id: Optional[str],
) -> Dict[str, str]:
    """For every node in g, return the non-seed anchor it's most-tied-to.

    The seed (Jukka) is excluded from candidate anchors during the search
    so that nodes prefer to belong to *whoever else* they're connected to.
    "Nicole is Katy's friend, not Jukka's" — even though Jukka touches
    Nicole transitively (he touches everyone), Nicole's spatial home is
    Katy's neighborhood. The seed only collects nodes that have no other
    anchor reachable within 2 hops (Jukka's personal-only material).

    Score = best (1-hop edge weight) or, if no direct non-seed anchor
    edge, best (2-hop edge-weight product × 0.5 discount).
    """
    anchor_set = set(anchors)
    candidate_anchors = anchor_set - ({seed_id} if seed_id else set())
    primary: Dict[str, str] = {a: a for a in anchors}

    for nid in g.nodes:
        if nid in anchor_set:
            continue
        best_anchor: Optional[str] = None
        best_score = -1.0

        # 1-hop: direct edge to any non-seed anchor.
        for nbr in g.neighbors(nid):
            if nbr in candidate_anchors:
                w = float(g[nid][nbr].get("weight", 1.0))
                if w > best_score:
                    best_score = w
                    best_anchor = nbr

        # 2-hop: through any neighbor (including seed) to a non-seed anchor.
        if best_anchor is None:
            for nbr in g.neighbors(nid):
                w1 = float(g[nid][nbr].get("weight", 1.0))
                for nbr2 in g.neighbors(nbr):
                    if nbr2 == nid or nbr2 not in candidate_anchors:
                        continue
                    w2 = float(g[nbr][nbr2].get("weight", 1.0))
                    score = w1 * w2 * 0.5
                    if score > best_score:
                        best_score = score
                        best_anchor = nbr2

        # Fallback: the seed (if any), or the last non-seed anchor.
        if best_anchor is None:
            if seed_id and seed_id in anchor_set:
                best_anchor = seed_id
            else:
                best_anchor = anchors[-1] if anchors else nid
        primary[nid] = best_anchor
    return primary


def _anchor_positions(
    anchors: List[str],
    seed_id: Optional[str],
    cluster_sizes: Dict[str, int],
) -> Dict[str, Tuple[float, float]]:
    """Place anchors so each gets real estate proportional to its cluster.

    Seed sits at origin. Other anchors spiral out by golden angle, with
    the radial distance growing as sqrt(cumulative mass) — where mass
    counts the anchor itself plus its primary-cluster size. So an anchor
    with a 50-member neighborhood pushes the spiral outward 50× more
    than a singleton, and itself sits far enough from origin that its
    neighborhood doesn't crowd the seed.
    """
    positions: Dict[str, Tuple[float, float]] = {}
    non_seed = [a for a in anchors if a != seed_id]
    if seed_id and seed_id in anchors:
        positions[seed_id] = (0.0, 0.0)

    # Mass of an anchor = its own slot + cluster size.
    def mass(a: str) -> float:
        return float(cluster_sizes.get(a, 0) + 1)

    cumulative = mass(seed_id) if seed_id and seed_id in anchors else 1.0
    for i, a in enumerate(non_seed):
        # Add half this anchor's mass before placement (so it sits at the
        # midpoint of its allocated radial band), then add the rest after.
        cumulative += mass(a) / 2.0
        r = ANCHOR_RADIUS_PER_MASS * math.sqrt(cumulative) * INTER_ANCHOR_PADDING
        theta = i * GOLDEN_ANGLE
        positions[a] = (r * math.cos(theta), r * math.sin(theta))
        cumulative += mass(a) / 2.0
    return positions


def _layout_anchor_rooted(
    g: nx.Graph,
    importance: Dict[str, float],
    seed_id: Optional[str],
) -> Dict[str, Tuple[float, float]]:
    positions: Dict[str, Tuple[float, float]] = {}
    if g.number_of_nodes() == 0:
        return positions

    anchors = _pick_anchors(g, importance, seed_id, N_ANCHORS)
    if not anchors:
        # No persons at all — fall back to one big spring layout.
        try:
            local = nx.spring_layout(g, k=2.5, iterations=120, seed=42)
            return {nid: (float(p[0]) * 4000, float(p[1]) * 4000) for nid, p in local.items()}
        except Exception as e:
            logger.error("spring_layout fallback failed: %s", e)
            return {nid: _empty_position() for nid in g.nodes}

    primary = _primary_anchor_map(g, anchors, seed_id)

    # Group nodes by their primary anchor (so we know cluster sizes
    # *before* placing anchors).
    by_anchor: Dict[str, List[str]] = defaultdict(list)
    for nid, a in primary.items():
        by_anchor[a].append(nid)
    cluster_sizes = {a: len(members) for a, members in by_anchor.items()}

    anchor_pos = _anchor_positions(anchors, seed_id, cluster_sizes)

    logger.info(
        "me layout: cluster sizes — %s",
        sorted(
            ((g.nodes[a].get("label", a[:8]), cluster_sizes.get(a, 0)) for a in anchors),
            key=lambda x: -x[1],
        )[:10],
    )

    # Build a node_type lookup once for the deterministic Vogel placer.
    node_type_of = {nid: g.nodes[nid].get("node_type", "") for nid in g.nodes}

    # Lay out each anchor's cluster deterministically: most-important members
    # closest to the anchor, golden-angle spaced, radius scaled by cumulative
    # bbox-area so no two boxes overlap at "fully revealed" zoom.
    for a, members in by_anchor.items():
        anchor_pos_xy = anchor_pos.get(a, (0.0, 0.0))
        cluster_positions = _vogel_place_cluster(
            members=members,
            importance=importance,
            node_type_of=node_type_of,
            anchor_id=a,
            anchor_pos=anchor_pos_xy,
        )
        positions.update(cluster_positions)

    # Catch unplaced (shouldn't happen).
    placed = set(positions.keys())
    if len(placed) < g.number_of_nodes():
        unplaced = [n for n in g.nodes if n not in placed]
        logger.warning("me layout: %d unplaced nodes — scattering on outer ring", len(unplaced))
        for j, nid in enumerate(unplaced):
            ang = j * GOLDEN_ANGLE
            r = ORPHAN_RING_RADIUS + j * 30
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
    return get_layout(categories).get(node_id, _empty_position())


if __name__ == "__main__":  # pragma: no cover
    import app.assistant.tests.test_setup  # noqa: F401
    regenerate_layout()
