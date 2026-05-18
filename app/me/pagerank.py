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
# Effectively unlimited — the lens wants the entire positioned map so
# zoom-LOD can reveal more nodes the closer you zoom in (fractal style).
# Importance threshold + viewport cull do the actual culling at render time.
HARD_LIMIT = 20000

# Cache: (sorted seeds tuple, time_mode, time_from, time_to) → (timestamp, result)
_CACHE: Dict[Tuple, Tuple[float, "SeedGraphResult"]] = {}
_CACHE_TTL_SECONDS = 30


@dataclass
class SeedGraphResult:
    nodes: List[Dict]
    edges: List[Dict]
    bridge_edges: List[Dict]
    pagerank: Dict[str, float]
    seeds: List[str]
    time_mode: str
    time_from: Optional[str]
    time_to: Optional[str]
    total_candidates: int


def _lod_tier_for(node: "Node") -> int:
    """LOD tier: 0 = always shown (persons + seeds), 1 = mid-zoom (states/events
    /goals — the connective tissue), 2 = close-zoom only (everything else).
    """
    if node is None:
        return 2
    if _is_person(node):
        return 0
    nt = (node.node_type or "")
    if nt in ("State", "Event", "Goal"):
        return 1
    return 2


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

    # Entity nodes (people, places, organizations) are persistent. Any
    # dates that happen to live on their row come from extraction noise
    # ("Katy was first mentioned on 2026-03-12") and don't bound her
    # existence. Only State/Event/Goal nodes are genuinely time-bounded.
    if (node.node_type or "") == "Entity":
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


def _is_person(n: "Node") -> bool:
    return (
        n is not None
        and (n.node_type or "") == "Entity"
        and (n.category or "").lower().strip() == "person"
    )


def _compute_bridge_edges(
    visible: Set[str],
    g: "nx.Graph",
    node_by_id: Dict[str, "Node"],
    *,
    max_per_pair: int = 2,
    seed_set: Optional[Set[str]] = None,
) -> List[Dict]:
    """Synthetic entity↔entity edges that route via shared State/Event/Goal nodes.

    KG modeling rule: two entities rarely connect directly — most
    relationships run through an intermediating State/Event/Goal
    (Marriage, Possession, Comparative-Belief, ...). When the camera
    can't show the bridging state (zoomed too far in for the state's
    midpoint position to be in viewport, or zoomed too far out and the
    state was culled by the LOD), the two endpoint entities would look
    disconnected. Bridge edges are synthetic direct lines between the
    two entities labeled with the state's name, drawn in addition to or
    instead of the real state-mediated edges depending on the frontend's
    bridge-mode flag.

    Was previously person↔person only — that left non-person entities
    (Bike, School, Most Doctors, San Diego Credit Union, …) without any
    fallback line when their bridging state dropped out. Generalized to
    all entity types for connectivity-completeness.

    Returns {id, source_id, target_id, via_node_id, label} dicts. Capped
    to ``max_per_pair`` bridges per pair to avoid clutter when two
    entities share many states.
    """
    BRIDGE_VIA_TYPES = {"State", "Event", "Goal"}
    # Bridges are entity-to-entity. Skip the bridging via-types themselves
    # as endpoints (a State doesn't bridge to another State via a third
    # State — that's just graph noise) and skip Concepts (they're scaffold,
    # not the kind of node a user expects a labeled connection to).
    entity_ids: List[str] = [
        nid for nid in visible
        if (n := node_by_id.get(nid)) is not None
        and (n.node_type or "") == "Entity"
    ]
    bridges: List[Dict] = []
    seed_set = seed_set or set()

    # Bridges only radiate FROM the seed. Without this restriction
    # entity-pair bridges generate for any two visible entities sharing a
    # state, which surfaced edges like "Haliburton ↔ Legoland via some
    # travel-state" — entities that have nothing to do with Jukka but
    # happen to share a neighbor in his admitted scope. The lens is
    # focal-node-centric: every line on screen should be Jukka's line.
    for i, a in enumerate(entity_ids):
        if not g.has_node(a):
            continue
        a_neighbors = set(g.neighbors(a))
        for b in entity_ids[i + 1:]:
            if not g.has_node(b):
                continue
            # Require one endpoint to be a seed.
            if seed_set and (a not in seed_set) and (b not in seed_set):
                continue
            shared = a_neighbors & set(g.neighbors(b))
            if not shared:
                continue
            count_for_pair = 0
            for via in shared:
                via_node = node_by_id.get(via)
                if via_node is None:
                    continue
                if (via_node.node_type or "") not in BRIDGE_VIA_TYPES:
                    continue
                bridges.append({
                    "id": f"bridge:{a[:8]}:{b[:8]}:{via[:8]}",
                    "source_id": a,
                    "target_id": b,
                    "via_node_id": via,
                    "label": via_node.label or "",
                })
                count_for_pair += 1
                if count_for_pair >= max_per_pair:
                    break

    return bridges


def _pick_anchors_around_seed(
    seed_id: str,
    g: "nx.Graph",
    node_by_id: Dict[str, "Node"],
    *,
    k: int = 6,
) -> List[str]:
    """Promote the top-K persons in the seed's 2-hop neighborhood to anchors.

    Anchors become layout cluster centers — each gets its own district on
    the canvas. Restricted to category=person so routines / phone numbers /
    devices / lists never get promoted, even if they have heavy KG edges to
    the seed. Scored by LLM-rated importance, NOT by raw edge weight, so
    family beats a daily-routine even when the daily-routine has more
    extracted KG edges.

    2-hop coverage matters because Jorma/Sinikka often connect to Jukka
    through State nodes (shared experiences) rather than directly.
    """
    from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE
    importance = get_importance_map()

    if not g.has_node(seed_id):
        return []

    # Collect all nodes within 2 hops of the seed.
    one_hop = set(g.neighbors(seed_id))
    two_hop: Set[str] = set()
    for nb in one_hop:
        for nb2 in g.neighbors(nb):
            if nb2 != seed_id and nb2 not in one_hop:
                two_hop.add(nb2)
    candidate_ids = one_hop | two_hop

    candidates: List[tuple] = []
    for nid in candidate_ids:
        n = node_by_id.get(nid)
        if n is None:
            continue
        if (n.node_type or "") != "Entity":
            continue
        score = float(importance.get(nid, DEFAULT_SCORE))
        candidates.append((score, nid))
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
    categories: Optional[List[str]] = None,
    max_graph_hops: Optional[int] = 2,
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

    cat_filter: Optional[Set[str]] = None
    if categories:
        cat_filter = {c.lower().strip() for c in categories if c}
        if not cat_filter:
            cat_filter = None

    cache_key = (
        tuple(sorted(seed_ids)),
        time_mode,
        time_from or "",
        time_to or "",
        limit,
        state_score_multiplier,
        include_concepts,
        auto_anchors_per_seed,
        tuple(sorted(cat_filter)) if cat_filter else (),
        max_graph_hops,
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
            nid_str = str(n.id)
            is_seed_self = nid_str in seed_set_for_filter
            if not _node_active_in_window(n, time_mode, tf, tt):
                continue
            # Concept nodes are pure taxonomy scaffolding ("animal",
            # "career", "city") — visual noise in the personal lens.
            # Excluded by default; opt-in via include_concepts when the
            # user explicitly asks. Seeds always survive the filter.
            if (
                not include_concepts
                and (n.node_type or "") == "Concept"
                and not is_seed_self
            ):
                continue
            # Category filter (experimental persons-only mode). Seeds
            # always survive even if their category doesn't match —
            # otherwise the user's own seed could be filtered away.
            if cat_filter is not None and not is_seed_self:
                if (n.node_type or "") != "Entity":
                    continue
                if (n.category or "").lower().strip() not in cat_filter:
                    continue
            node_by_id[nid_str] = n

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
            empty = SeedGraphResult([], [], [], {}, seed_ids, time_mode, time_from, time_to, 0)
            _CACHE[cache_key] = (now, empty)
            return empty

        # Filter seeds to those that survived the time filter.
        valid_seeds = [s for s in seed_ids if s in node_by_id]

        # Hop scoping. The KG wires entities through state nodes
        # (entity → State → entity), so 2 graph hops = 1 entity hop =
        # "Jukka's immediate orbit." By default we cut everything past
        # `max_graph_hops` from any seed — this is the right cut for the
        # current concentric-ring layout and visibility design, both of
        # which were built around a single focal anchor and its direct
        # neighbors. Looking past 1 entity hop needs a different layout
        # story (multi-anchor districts, intersection views, etc.) that
        # we haven't built yet. Pass max_graph_hops=None to opt out of
        # the cut and see the full reachable cone.
        if max_graph_hops is not None and valid_seeds:
            reachable: Set[str] = set()
            for sid in valid_seeds:
                if g.has_node(sid):
                    reachable.update(
                        nx.single_source_shortest_path_length(
                            g, sid, cutoff=int(max_graph_hops),
                        ).keys()
                    )
            node_by_id = {nid: n for nid, n in node_by_id.items() if nid in reachable}
            g = g.subgraph(reachable).copy()
            active_edges = [
                e for e in active_edges
                if str(e.source_id) in reachable and str(e.target_id) in reachable
            ]
            if g.number_of_nodes() == 0:
                empty = SeedGraphResult([], [], [], {}, seed_ids, time_mode, time_from, time_to, 0)
                _CACHE[cache_key] = (now, empty)
                return empty

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

        # Blend LLM importance (dominant) with PageRank (tiebreaker).
        # The user wants importance to lead the ranking: a routine Jukka
        # interacts with daily should not outrank Sinikka just because
        # Jukka's personalization pumps PageRank into the routine. So
        # importance (0-10) is the primary score; PR contributes at most
        # +0.5 — a tiebreaker that disambiguates among nodes of similar
        # importance, never enough to leapfrog a calibration band.
        from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE
        importance_map = dict(get_importance_map())

        # Ownership cap: a non-person Entity (phone number, address, device,
        # account, etc.) cannot be more important than the highest-rated
        # person it touches, scaled down. Without this the rater can score
        # "Katy's phone" 9.5 just because Katy is 9.5 — the structure of the
        # KG already says the phone is downstream of Katy, so we enforce
        # phone < Katy in importance regardless of the rater's score.
        OWNERSHIP_CAP_FACTOR = 0.7
        for nid in list(g.nodes):
            n = node_by_id.get(nid)
            if n is None or (n.node_type or "") != "Entity":
                continue
            if (n.category or "").lower().strip() == "person":
                continue
            max_owner = 0.0
            for nbr in g.neighbors(nid):
                nbr_n = node_by_id.get(nbr)
                if nbr_n is None:
                    continue
                if (nbr_n.category or "").lower().strip() != "person":
                    continue
                max_owner = max(max_owner, importance_map.get(nbr, DEFAULT_SCORE))
            if max_owner > 0:
                cap = max_owner * OWNERSHIP_CAP_FACTOR
                cur = importance_map.get(nid, DEFAULT_SCORE)
                if cur > cap:
                    importance_map[nid] = cap

        max_pr = max(scores.values()) if scores else 1.0
        max_pr = max(max_pr, 1e-9)
        PR_TIEBREAKER_WEIGHT = 0.5  # at most this much added to importance

        biased: Dict[str, float] = {}
        seed_set: Set[str] = set(valid_seeds)
        for nid, score in scores.items():
            if nid in seed_set:
                biased[nid] = 100.0  # seed always ranks first; arbitrary high
                continue
            n = node_by_id.get(nid)
            imp = float(importance_map.get(nid, DEFAULT_SCORE))  # 0-10
            pr_contrib = (score / max_pr) * PR_TIEBREAKER_WEIGHT
            blended = imp + pr_contrib
            if n is not None and (n.node_type or "") in STATE_NODE_TYPES:
                # States are connective tissue, never co-equal with persons.
                # Multiplying by state_score_multiplier (0.4) keeps them
                # below the lowest-importance Entity in the visible cap.
                blended = blended * state_score_multiplier
            biased[nid] = blended

        # Tier-priority top-K selection. Persons are admitted FIRST so that
        # the lens never shows "Mewgenics" while hiding "Katy" — a structural
        # answer to the importance-ranking gap. Order:
        #   Phase A: all persons (Entity + category=person), ranked by PR.
        #   Phase B: bridge State/Event/Goal nodes connecting ≥2 visible.
        #   Phase C: state/goal touching a seed directly (anchor connector).
        #   Phase D: remaining non-person Entity nodes by PR (foods, software,
        #            video games, places, etc.) — only if cap not yet hit.
        ranked = sorted(biased.items(), key=lambda kv: (-kv[1], kv[0]))
        visible: Set[str] = set(valid_seeds)

        # A: persons.
        for nid, _score in ranked:
            if len(visible) >= limit:
                break
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None:
                continue
            if not _is_person(n):
                continue
            visible.add(nid)

        # B: bridge state/goal nodes (connect ≥2 visible).
        for nid, _score in ranked:
            if len(visible) >= limit:
                break
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None or (n.node_type or "") not in STATE_NODE_TYPES:
                continue
            try:
                neighbors = list(g.neighbors(nid))
            except nx.NetworkXError:
                continue
            visible_neighbors = sum(1 for nb in neighbors if nb in visible)
            if visible_neighbors >= 2:
                visible.add(nid)

        # C: state/goal touching a seed directly.
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

        # D: remaining non-person Entity nodes by PR. Only if there's room.
        for nid, _score in ranked:
            if len(visible) >= limit:
                break
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None:
                continue
            if (n.node_type or "") != "Entity":
                continue
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

        # Connective-tissue pass: admit every State/Event/Goal that bridges
        # ≥2 already-visible nodes. Uncapped — the count is naturally
        # bounded by the visible edge count, and previous attempts at
        # capping by state-importance left the lowest-importance entities
        # stranded (their bridging states are themselves low-importance,
        # so they lost the cap competition). Connectivity is not optional;
        # if we admitted the entity we owe it its bridging state.
        #
        # Most KG relationships go entity→State→entity (Marriage,
        # Possession, Struggling, ...). Without admitting the State, the
        # edge filter below drops both legs of the path and the endpoint
        # entity looks orphaned even though it has a real connection in
        # the KG.
        for nid, _score in ranked:
            if nid in visible:
                continue
            n = node_by_id.get(nid)
            if n is None or (n.node_type or "") not in STATE_NODE_TYPES:
                continue
            try:
                neighbors = list(g.neighbors(nid))
            except nx.NetworkXError:
                continue
            visible_neighbors = sum(1 for nb in neighbors if nb in visible)
            if visible_neighbors >= 2:
                visible.add(nid)

        # Inter-visible edges only — AND additionally restrict to edges
        # that are within 1 graph hop of a seed (i.e., at least one
        # endpoint is a seed or a direct neighbor of a seed). This is the
        # "every line on screen passes through Jukka" rule: state-mediated
        # paths from Jukka show, but edges between two non-Jukka entities
        # (or between two states neither of which touches Jukka) are
        # filtered out. Without this we'd surface things like a Visit
        # edge between two travel destinations because they happen to
        # share a state that's somewhere downstream of Jukka.
        seed_neighbor_set: Set[str] = set()
        for sid in valid_seeds:
            if g.has_node(sid):
                seed_neighbor_set.update(g.neighbors(sid))
        seed_or_neighbor: Set[str] = set(valid_seeds) | seed_neighbor_set

        edges_out: List[Dict] = []
        seen_edge_ids: Set[str] = set()
        for e in active_edges:
            sid = str(e.source_id)
            tid = str(e.target_id)
            if sid not in visible or tid not in visible:
                continue
            # Require at least one endpoint to be the seed itself or a
            # direct (graph-distance-1) neighbor of a seed.
            if seed_or_neighbor and sid not in seed_or_neighbor and tid not in seed_or_neighbor:
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
        # Layout is per-category-filter — persons-only mode has its own.
        from app.me.layout import get_layout
        layout = get_layout(categories=list(cat_filter) if cat_filter else None)

        # Bridge edges: synthetic seed↔entity connectors via shared
        # State/Event/Goal neighbors. Used when the via-state isn't
        # visible (zoomed out, off viewport). Restricted to bridges with
        # the seed as one endpoint — entity↔entity bridges between two
        # non-seed entities surfaced spurious lines like Haliburton↔
        # Legoland just because they shared some travel state.
        bridge_edges_out = _compute_bridge_edges(
            visible, g, node_by_id, seed_set=set(valid_seeds),
        )

        # Per-type defaults so unrated nodes don't all bunch at 5.0 and
        # cause a visibility cliff right at the calibration midpoint.
        # The rater scores Entity nodes; for everything else, give a
        # type-appropriate default that spreads them across the threshold
        # range so reveal feels gradual rather than a step function.
        TYPE_DEFAULT_IMPORTANCE: Dict[str, float] = {
            "Entity": 2.0,  # rare — most Entities are rated
            "State": 4.0,   # connective tissue, mid-zoom
            "Event": 3.0,   # episodic
            "Goal": 3.0,
            "Concept": 0.5, # taxonomy scaffolding
        }

        nodes_out: List[Dict] = []
        for nid in visible:
            n = node_by_id[nid]
            pos = layout.get(nid, (0.0, 0.0))
            rated = importance_map.get(nid)
            if rated is None:
                llm_imp = TYPE_DEFAULT_IMPORTANCE.get(str(n.node_type or ""), 2.0)
            else:
                llm_imp = float(rated)
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
                "llm_importance": float(llm_imp),
                "pagerank_score": float(scores.get(nid, 0.0)),
                "is_seed": nid in seed_set,
                "is_anchor": nid in anchor_set,
                "primary_anchor_id": primary_by_node.get(nid),
                "lod_tier": _lod_tier_for(n),
                "x": float(pos[0]),
                "y": float(pos[1]),
            })

        result = SeedGraphResult(
            nodes=nodes_out,
            edges=edges_out,
            bridge_edges=bridge_edges_out,
            pagerank={n["id"]: n["pagerank_score"] for n in nodes_out},
            seeds=valid_seeds,
            time_mode=time_mode,
            time_from=time_from,
            time_to=time_to,
            total_candidates=len(node_by_id),
        )
        _CACHE[cache_key] = (now, result)

        logger.info(
            "compute_seed_graph: seeds=%s mode=%s window=%s..%s → %d/%d nodes, %d edges, %d bridges",
            valid_seeds, time_mode, time_from, time_to,
            len(nodes_out), len(node_by_id), len(edges_out), len(bridge_edges_out),
        )
        return result


def invalidate_cache() -> None:
    """Drop the cache. Call after any KG mutation."""
    _CACHE.clear()
