"""Frozen organic 3D focus-graph layout for KG Lens.

The KG Lens viewing model (user spec, 2026-06-11):

- Focus one node — or SEVERAL ("nodes connected to both Alex and Sam").
  The attached neighborhood is returned in one payload, every node carrying
  a precomputed (x, y, z) position.
- The client renders only nodes whose importance clears a threshold;
  ctrl+scroll moves the threshold (a PURE visibility filter — show/hide in
  place), plain scroll is camera zoom. The two are fully independent, and
  NOTHING is ever re-laid-out at view time: revealing more nodes just
  un-hides nodes that already have homes.

Multi-focus semantics: with one focus you get its full neighborhood; with
several (up to 3) you get the INTERSECTION — only nodes attached to every
focus (shared kids, the marriage state, common trips). Foci are pinned
apart on a line/triangle so the shared structure hangs between them.

Layout: one seeded 3D force simulation (networkx spring layout) over the
whole result — the organic kg-visualizer look, where related nodes clump
naturally. Foci are pinned and the RNG seed plus deterministic node/edge
ordering make the result reproducible: the same neighborhood always
settles into the same shape. Importance is NOT encoded in position; the
client shows it via node size/brightness.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import networkx as nx
from sqlalchemy import or_

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

ENVELOPE_TYPES = frozenset({"State", "Event", "Goal", "Property"})

DEFAULT_MAX_NODES = 400
HARD_MAX_NODES = 1500
MAX_FOCI = 3

# World scale of the frozen layout (graph coords; camera fits to content).
LAYOUT_SCALE = 1400.0
LAYOUT_SEED = 42
LAYOUT_ITERATIONS = 60

# Cache: (foci tuple, mode, max_nodes) → (timestamp, payload). Short TTL —
# the graph mutates under us through the audited mutator (including from
# the Lens itself; an edit should be visible on the next refocus).
_CACHE: Dict[Tuple, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 20


def _focus_anchor_positions(foci: List[str]) -> Dict[str, Tuple[float, float, float]]:
    """Pinned unit-space anchors: single focus at origin, two on a line,
    three on a triangle. The shared neighborhood settles between them."""
    n = len(foci)
    if n == 1:
        return {foci[0]: (0.0, 0.0, 0.0)}
    if n == 2:
        return {foci[0]: (-0.5, 0.0, 0.0), foci[1]: (0.5, 0.0, 0.0)}
    out: Dict[str, Tuple[float, float, float]] = {}
    for i, fid in enumerate(foci):
        theta = 2.0 * math.pi * i / n
        out[fid] = (0.5 * math.cos(theta), 0.5 * math.sin(theta), 0.0)
    return out


def compute_focus_graph(
    focus_ids: Iterable[str] | str,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    mode: str = "auto",
) -> Optional[Dict[str, Any]]:
    """Positioned neighborhood of one or more focus nodes.

    mode: 'auto' (intersection when multiple foci, plain neighborhood for
    one), 'intersection', or 'union'. Returns None when no focus exists.
    """
    if isinstance(focus_ids, str):
        focus_ids = [focus_ids]
    foci: List[str] = []
    for fid in focus_ids:
        fid = str(fid or "").strip()
        if fid and fid not in foci:
            foci.append(fid)
    foci = foci[:MAX_FOCI]
    if not foci:
        return None

    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "intersection", "union"):
        mode = "auto"
    effective_mode = "intersection" if (mode == "auto" and len(foci) > 1) else (
        mode if mode != "auto" else "union"
    )

    max_nodes = max(10, min(int(max_nodes), HARD_MAX_NODES))
    cache_key = (tuple(sorted(foci)), effective_mode, max_nodes)
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE
    importance_map = get_importance_map()

    def imp(node_id: str) -> float:
        return float(importance_map.get(node_id, DEFAULT_SCORE))

    started = time.monotonic()
    foci_set = set(foci)
    with get_db_manager().read_session() as session:
        focus_nodes: Dict[str, Node] = {}
        for n in session.query(Node).filter(Node.id.in_(foci)).all():
            focus_nodes[str(n.id)] = n
        if len(focus_nodes) != len(foci):
            return None

        # ---- per-focus traversal: 1 hop + far endpoints of envelopes ----
        all_candidates: Dict[str, Node] = {}
        per_focus_candidates: Dict[str, Set[str]] = {}
        collected_edges: List[Edge] = []
        all_envelope_ids: Set[str] = set()

        for fid in foci:
            edges_at_focus = (
                session.query(Edge)
                .filter((Edge.source_id == fid) | (Edge.target_id == fid))
                .all()
            )
            collected_edges.extend(edges_at_focus)
            one_hop_ids: Set[str] = set()
            for e in edges_at_focus:
                other = str(e.target_id) if str(e.source_id) == fid else str(e.source_id)
                if other not in foci_set:
                    one_hop_ids.add(other)

            one_hop_nodes: Dict[str, Node] = {}
            missing = one_hop_ids - set(all_candidates)
            if missing:
                for n in session.query(Node).filter(Node.id.in_(missing)).all():
                    one_hop_nodes[str(n.id)] = n
            for nid in one_hop_ids:
                if nid in all_candidates:
                    one_hop_nodes[nid] = all_candidates[nid]
            all_candidates.update(one_hop_nodes)

            envelope_ids = {
                nid for nid, n in one_hop_nodes.items()
                if (n.node_type or "") in ENVELOPE_TYPES
            }
            all_envelope_ids |= envelope_ids

            far_ids: Set[str] = set()
            if envelope_ids:
                envelope_edges = (
                    session.query(Edge)
                    .filter(or_(Edge.source_id.in_(envelope_ids), Edge.target_id.in_(envelope_ids)))
                    .all()
                )
                collected_edges.extend(envelope_edges)
                for e in envelope_edges:
                    sid, tid = str(e.source_id), str(e.target_id)
                    env = sid if sid in envelope_ids else (tid if tid in envelope_ids else None)
                    if env is None:
                        continue
                    other = tid if env == sid else sid
                    if other not in foci_set and other not in envelope_ids:
                        far_ids.add(other)

            missing_far = far_ids - set(all_candidates)
            if missing_far:
                for n in session.query(Node).filter(Node.id.in_(missing_far)).all():
                    all_candidates[str(n.id)] = n

            per_focus_candidates[fid] = one_hop_ids | far_ids

        # ---- combine per-focus sets ----
        if effective_mode == "intersection":
            combined: Set[str] = set.intersection(*per_focus_candidates.values())
        else:
            combined = set.union(*per_focus_candidates.values())
        combined &= set(all_candidates)

        # ---- cap by importance (keep the most important when huge) ----
        ranked = sorted(combined, key=lambda nid: (-imp(nid), nid))
        kept_ids = set(ranked[:max_nodes])
        dropped = len(ranked) - len(kept_ids)

        # ---- edges among kept ∪ foci ----
        included = kept_ids | foci_set
        edges_out: List[Dict[str, Any]] = []
        seen_edge_ids: Set[str] = set()

        def _add_edge(e: Edge) -> None:
            eid = str(e.id)
            if eid in seen_edge_ids:
                return
            sid, tid = str(e.source_id), str(e.target_id)
            if sid in included and tid in included and sid != tid:
                seen_edge_ids.add(eid)
                edges_out.append({
                    "id": eid,
                    "source_id": sid,
                    "target_id": tid,
                    "relationship_type": e.relationship_type or "",
                    "sentence": e.sentence or "",
                    "importance": float(e.importance or 0.5),
                    "confidence": float(e.confidence or 0.5),
                })

        for e in collected_edges:
            _add_edge(e)
        remaining = kept_ids - all_envelope_ids
        if remaining:
            for e in (
                session.query(Edge)
                .filter(Edge.source_id.in_(remaining), Edge.target_id.in_(remaining))
                .all()
            ):
                _add_edge(e)

        # ---- prune nodes the cap/intersection disconnected ----
        degree: Dict[str, int] = defaultdict(int)
        for e in edges_out:
            degree[e["source_id"]] += 1
            degree[e["target_id"]] += 1
        connected_ids = {nid for nid in kept_ids if degree[nid] > 0}
        pruned_isolated = len(kept_ids) - len(connected_ids)
        final_ids = connected_ids | foci_set
        edges_out = [
            e for e in edges_out
            if e["source_id"] in final_ids and e["target_id"] in final_ids
        ]

        # ---- frozen organic layout: one seeded 3D spring simulation ----
        graph = nx.Graph()
        # Deterministic node order matters: spring_layout's matrices follow
        # insertion order, so sort before adding.
        for fid in foci:
            graph.add_node(fid)
        for nid in sorted(connected_ids):
            graph.add_node(nid)
        for e in sorted(edges_out, key=lambda d: d["id"]):
            graph.add_edge(e["source_id"], e["target_id"])

        anchors = _focus_anchor_positions(foci)
        positions = nx.spring_layout(
            graph,
            dim=3,
            seed=LAYOUT_SEED,
            iterations=LAYOUT_ITERATIONS,
            pos={fid: tuple(p) for fid, p in anchors.items()},
            fixed=foci,
            scale=None,  # fixed=... forbids rescaling; we scale manually below
        )

        # Manual scale to world coords: median non-focus distance from the
        # anchor centroid → comfortable spacing, robust against stragglers.
        dists = sorted(
            math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2)
            for nid, p in positions.items() if nid not in foci_set
        )
        if dists:
            median = dists[len(dists) // 2] or 1.0
            factor = (LAYOUT_SCALE / 2.0) / median
        else:
            factor = LAYOUT_SCALE

        # ---- payload ----
        nodes_out: List[Dict[str, Any]] = []
        for nid, (x, y, z) in positions.items():
            node = focus_nodes.get(nid) or all_candidates.get(nid)
            if node is None:
                continue
            nodes_out.append({
                "id": nid,
                "label": node.label or "",
                "node_type": str(node.node_type or ""),
                "category": node.category,
                "importance": imp(nid),
                "node_importance": float(node.importance) if node.importance is not None else None,
                "is_focus": nid in foci_set,
                "aliases": list(node.aliases or []),
                "goal_status": (node.goal_status if (node.node_type or "") == "Goal" else None),
                "x": float(x) * factor,
                "y": float(y) * factor,
                "z": float(z) * factor,
            })

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[kg_lens] focus-graph %s (%s): %d nodes, %d edges (%d combined, %d capped, %d isolated) in %.0fms",
        "+".join(f[:8] for f in foci), effective_mode,
        len(nodes_out), len(edges_out), len(combined), dropped, pruned_isolated, elapsed_ms,
    )

    payload = {
        "focus_id": foci[0],
        "focus_ids": foci,
        "mode": effective_mode,
        "nodes": nodes_out,
        "edges": edges_out,
        "total_candidates": len(combined),
        "dropped_by_cap": max(0, dropped + pruned_isolated),
    }
    _CACHE[cache_key] = (time.time(), payload)
    return payload


def compute_set_graph(node_ids: Iterable[str], *, max_nodes: int = DEFAULT_MAX_NODES) -> Dict[str, Any]:
    """Frozen layout for an EXPLICIT node set (the agent's show_nodes intent).

    The agent computes any filter it likes via kg_query SQL and hands the
    exact ids here; we load them, collect the edges among them, and run the
    same seeded spring layout. No traversal, no foci — the set IS the view.
    Unknown ids are skipped (the graph may have mutated since the query).
    """
    ids: List[str] = []
    seen: Set[str] = set()
    for nid in node_ids:
        nid = str(nid or "").strip()
        if nid and nid not in seen:
            seen.add(nid)
            ids.append(nid)
    ids = ids[:max(10, min(int(max_nodes), HARD_MAX_NODES))]

    from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE
    importance_map = get_importance_map()

    def imp(node_id: str) -> float:
        return float(importance_map.get(node_id, DEFAULT_SCORE))

    started = time.monotonic()
    with get_db_manager().read_session() as session:
        found: Dict[str, Node] = {}
        if ids:
            for n in session.query(Node).filter(Node.id.in_(ids)).all():
                found[str(n.id)] = n

        edges_out: List[Dict[str, Any]] = []
        if found:
            id_set = set(found)
            for e in (
                session.query(Edge)
                .filter(Edge.source_id.in_(id_set), Edge.target_id.in_(id_set))
                .all()
            ):
                sid, tid = str(e.source_id), str(e.target_id)
                if sid == tid:
                    continue
                edges_out.append({
                    "id": str(e.id),
                    "source_id": sid,
                    "target_id": tid,
                    "relationship_type": e.relationship_type or "",
                    "sentence": e.sentence or "",
                    "importance": float(e.importance or 0.5),
                    "confidence": float(e.confidence or 0.5),
                })

        graph = nx.Graph()
        for nid in sorted(found):
            graph.add_node(nid)
        for e in sorted(edges_out, key=lambda d: d["id"]):
            graph.add_edge(e["source_id"], e["target_id"])

        if graph.number_of_nodes():
            positions = nx.spring_layout(
                graph, dim=3, seed=LAYOUT_SEED, iterations=LAYOUT_ITERATIONS,
                scale=LAYOUT_SCALE / 2.0,
            )
        else:
            positions = {}

        nodes_out: List[Dict[str, Any]] = []
        for nid, (x, y, z) in positions.items():
            node = found[nid]
            nodes_out.append({
                "id": nid,
                "label": node.label or "",
                "node_type": str(node.node_type or ""),
                "category": node.category,
                "importance": imp(nid),
                "node_importance": float(node.importance) if node.importance is not None else None,
                "is_focus": False,
                "aliases": list(node.aliases or []),
                "goal_status": (node.goal_status if (node.node_type or "") == "Goal" else None),
                "x": float(x),
                "y": float(y),
                "z": float(z),
            })

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[kg_lens] set-graph: %d/%d nodes, %d edges in %.0fms",
        len(nodes_out), len(ids), len(edges_out), elapsed_ms,
    )
    return {
        "focus_id": None,
        "focus_ids": [],
        "mode": "explicit_set",
        "nodes": nodes_out,
        "edges": edges_out,
        "total_candidates": len(ids),
        "dropped_by_cap": max(0, len(ids) - len(nodes_out)),
    }
