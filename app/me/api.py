"""REST blueprint for the lens (`/me`).

All endpoints are read-only or audit-friendly:
- /api/me/seed-graph: bounded subgraph from personalized PageRank.
- /api/me/node/<id>: node details + edge counts.
- /api/me/photo/<id>: photo bytes (cached, ETag-friendly).
- /api/me/parse-query: chat-input → seed/filter parameters.
- /api/me/flag: writes a kg_maintenance_finding for curatorial follow-up.
- /api/me/default-seed: returns the user's own node id (the empty-state seed).

No write endpoints touch the KG directly. Mutations route through the
audited typed-mutator pipeline (kg_dev_manager etc.) elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.time_utils import to_rfc3339_z, utc_now

from flask import Blueprint, jsonify, request

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.me.pagerank import compute_seed_graph, DEFAULT_LIMIT, HARD_LIMIT
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

me_api = Blueprint("me_api", __name__)


def _parse_csv_param(name: str) -> List[str]:
    raw = request.args.get(name, "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


@me_api.route("/api/me/demo-graph", methods=["GET"])
def demo_graph():
    """PoC endpoint: top-N entities around a single seed, laid out fresh.

    Ignores the global me_layout.json. Computes a Jukka-centric (or any
    seed) layout where the seed sits at origin and its top-N most-important
    1-hop entity neighbors orbit on a Vogel spiral, sorted by importance
    descending (most important closest to seed).

    Used to validate the claim-collision visibility on a small set
    without 6000 nodes muddying the picture.

    Query params:
      seed   single seed node id. Defaults to the user's own node.
      n      max neighbors to include. Default 10, max 50.
    """
    import math
    from app.assistant.importance.cache import get_importance_map, DEFAULT_SCORE

    seed_id = (request.args.get("seed") or "").strip()
    if not seed_id:
        seed_id = _resolve_default_seed_id() or ""
    if not seed_id:
        return jsonify({"error": "no seed"}), 400

    try:
        n = int(request.args.get("n", 10))
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 50))

    importance = get_importance_map()

    with get_db_manager().read_session() as session:
        seed_node = session.query(Node).filter(Node.id == seed_id).first()
        if seed_node is None:
            return jsonify({"error": "seed not found"}), 404

        # 1-hop entity neighbors via shared state/event/goal envelopes
        # (= 2-hop physical). Get all 1-hop neighbors, then for each State/
        # Event/Goal neighbor get their other endpoint.
        edges_at_seed = (
            session.query(Edge)
            .filter((Edge.source_id == seed_id) | (Edge.target_id == seed_id))
            .all()
        )
        one_hop_ids: set = set()
        for e in edges_at_seed:
            other = str(e.target_id) if str(e.source_id) == seed_id else str(e.source_id)
            one_hop_ids.add(other)
        # Through state/event/goal nodes, find the other entity.
        two_hop_entity_ids: set = set()
        for nid in one_hop_ids:
            n_obj = session.query(Node).filter(Node.id == nid).first()
            if n_obj is None or (n_obj.node_type or "") == "Entity":
                continue  # one-hop entity already counted
            # State/Event/Goal — look for its other endpoints
            for e in session.query(Edge).filter(
                (Edge.source_id == nid) | (Edge.target_id == nid)
            ).all():
                other = str(e.target_id) if str(e.source_id) == nid else str(e.source_id)
                if other == seed_id:
                    continue
                ne = session.query(Node).filter(Node.id == other).first()
                if ne is not None and (ne.node_type or "") == "Entity":
                    two_hop_entity_ids.add(other)

        # Combine: direct 1-hop entities + 2-hop entities via state envelopes.
        candidate_entity_ids = {
            nid for nid in one_hop_ids
            if (q := session.query(Node).filter(Node.id == nid).first()) is not None
            and (q.node_type or "") == "Entity"
        } | two_hop_entity_ids
        candidate_entity_ids.discard(seed_id)

        # Selection: take a *spread* across importance bands instead of
        # just top-N. With Jukka's neighborhood, top-N alone yields all
        # 9-9.5 family nodes — rings collapse. By picking a few from
        # each integer importance band, we get ring spacing that
        # actually exercises the layout.
        from collections import defaultdict as _dd
        per_band: Dict[int, List[str]] = _dd(list)
        for nid in candidate_entity_ids:
            imp = float(importance.get(nid, DEFAULT_SCORE))
            band = max(0, min(10, int(round(imp))))
            per_band[band].append(nid)
        # Sort each band's members by importance desc and take a slice.
        per_band_cap = max(1, n // 6)  # ~n/6 per band, ≥1
        ranked: List[str] = []
        for band in sorted(per_band.keys(), reverse=True):
            members = sorted(
                per_band[band],
                key=lambda nid: -float(importance.get(nid, DEFAULT_SCORE)),
            )
            ranked.extend(members[:per_band_cap])
            if len(ranked) >= n:
                break
        ranked = ranked[:n]

        # Concentric-rings layout: anchor at center, importance bins are
        # rings around it. Less-important nodes orbit closer to the
        # anchor (inner rings) — discoverable by zooming in. Important
        # nodes orbit farther out — visible at overview zoom via
        # priority-collision.
        # Radial mapping non-linear so the dense low-importance bands
        # get more area: r = R_OUTER × (imp/10)^EXPONENT where exponent
        # < 1 expands the low-imp band.
        from collections import defaultdict
        # Geometric-progression ring radii (validated in layout_sim.py):
        # r_i = R_BASE × RING_RATIO^(imp - 1).
        # Less important closer to anchor; each step out is 1.6× the
        # previous, so when the comfortably-displayed ring (500px) moves
        # to periphery (800px), the next inner ring reaches 500px.
        R_BASE = 300.0
        RING_RATIO = 1.6
        GOLDEN = math.pi * (3 - math.sqrt(5))

        def radius_for(imp: float) -> float:
            return R_BASE * (RING_RATIO ** max(0.0, float(imp) - 1.0))

        # Bin by integer importance (10 bins: 0..10).
        by_band: Dict[int, List[str]] = defaultdict(list)
        for nid in ranked:
            imp = float(importance.get(nid, DEFAULT_SCORE))
            band = max(0, min(10, int(round(imp))))
            by_band[band].append(nid)

        positions: Dict[str, tuple] = {seed_id: (0.0, 0.0)}
        # Stable angular start per ring (golden-angle by ring index)
        # so adjacent rings don't clump on the same axis.
        for band, members in by_band.items():
            r = radius_for(float(band))
            theta_start = band * GOLDEN
            slot_step = (2 * math.pi) / max(1, len(members))
            for i, nid in enumerate(members):
                theta = theta_start + i * slot_step
                positions[nid] = (r * math.cos(theta), r * math.sin(theta))

        # State/Event/Goal nodes: fetch states adjacent to ANY pair of
        # currently-visible entities. Place each at the midpoint of its
        # two endpoints; if multiple states share the same endpoint pair,
        # fan them perpendicular to the connecting line so they don't stack.
        # Limit to MAX_STATES_PER_PAIR to keep things uncluttered.
        visible_entity_ids = set([seed_id, *ranked])
        MAX_STATES_PER_PAIR = 3
        # Group states by their (endpoint_a, endpoint_b) pair, keyed with
        # the strongest edge (importance) for ranking.
        state_candidates: Dict[tuple, List[tuple]] = _dd(list)  # pair → [(score, state_nid)]
        state_endpoints: Dict[str, tuple] = {}
        for state_nid in one_hop_ids:
            state_node = session.query(Node).filter(Node.id == state_nid).first()
            if state_node is None or (state_node.node_type or "") not in (
                "State", "Event", "Goal"
            ):
                continue
            # Find this state's other entity endpoint (besides seed) AND
            # the strongest edge importance touching this state.
            other_entity = None
            best_edge_imp = 0.0
            for e in session.query(Edge).filter(
                (Edge.source_id == state_nid) | (Edge.target_id == state_nid)
            ).all():
                other = str(e.target_id) if str(e.source_id) == state_nid else str(e.source_id)
                edge_imp = float(e.importance or 5.0)
                if edge_imp > best_edge_imp:
                    best_edge_imp = edge_imp
                if other == seed_id:
                    continue
                if other in visible_entity_ids and other_entity is None:
                    other_entity = other
            if other_entity is None:
                continue  # state doesn't bridge two visible entities
            pair = tuple(sorted([seed_id, other_entity]))
            # Score for ranking within a pair-group.
            state_candidates[pair].append((best_edge_imp, state_nid))

        # Apply per-pair cap.
        state_groups: Dict[tuple, List[str]] = {}
        for pair, candidates in state_candidates.items():
            candidates.sort(reverse=True)  # highest edge importance first
            kept = [sid for _, sid in candidates[:MAX_STATES_PER_PAIR]]
            state_groups[pair] = kept
            for sid in kept:
                state_endpoints[sid] = pair

        # Place states: midpoint + perpendicular fan.
        FAN_SPACING = 450.0  # graph-coord spacing between fanned states
        for pair, states in state_groups.items():
            a_id, b_id = pair
            ax, ay = positions[a_id]
            bx, by = positions[b_id]
            mx, my = (ax + bx) / 2, (ay + by) / 2
            # Perpendicular direction (rotate 90°).
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length < 1e-6:
                px, py = 0.0, 1.0
            else:
                px, py = -dy / length, dx / length
            n_states = len(states)
            for i, s_nid in enumerate(states):
                offset = (i - (n_states - 1) / 2.0) * FAN_SPACING
                positions[s_nid] = (mx + px * offset, my + py * offset)

        # Post-placement collision resolution for states. Different
        # entity-pairs can produce midpoints near each other even after
        # the perpendicular fan separates within-pair states. Iteratively
        # push too-close state-pairs apart until none collide.
        state_ids = list(state_endpoints.keys())
        MIN_STATE_SEPARATION = 500.0
        for _ in range(30):
            moved = False
            for i, sa in enumerate(state_ids):
                ax, ay = positions[sa]
                for sb in state_ids[i + 1:]:
                    bx, by = positions[sb]
                    dx, dy = bx - ax, by - ay
                    d = math.hypot(dx, dy)
                    if d < MIN_STATE_SEPARATION and d > 1e-3:
                        push = (MIN_STATE_SEPARATION - d) / 2.0
                        ux, uy = dx / d, dy / d
                        positions[sa] = (ax - ux * push, ay - uy * push)
                        positions[sb] = (bx + ux * push, by + uy * push)
                        ax, ay = positions[sa]
                        moved = True
            if not moved:
                break

        # Build response.
        all_ids = [seed_id] + ranked + list(state_endpoints.keys())
        nodes_out = []
        for nid in all_ids:
            node = session.query(Node).filter(Node.id == nid).first()
            if node is None:
                continue
            x, y = positions[nid]
            # goal_status is a first-class column on Node for Goal nodes
            # (active / dormant / completed / abandoned). Surface it on
            # the lens payload so the frontend can dim or hide non-active
            # Goals.
            goal_status = (node.goal_status if (node.node_type or "") == "Goal" else None)
            nodes_out.append({
                "id": nid,
                "label": node.label or "",
                "node_type": str(node.node_type or ""),
                "category": node.category,
                "description": node.description or "",
                "llm_importance": float(importance.get(nid, DEFAULT_SCORE)),
                "is_seed": nid == seed_id,
                "is_anchor": False,
                "primary_anchor_id": seed_id,
                "x": x,
                "y": y,
                "pagerank_score": 0.0,
                "importance": float(node.importance or 0.5),
                "aliases": list(node.aliases or []),
                "start_date": None,
                "end_date": None,
                "goal_status": goal_status,
            })

        # Edges between visible nodes only (so the demo isn't cluttered).
        visible_set = set(all_ids)
        edges_out = []
        for e in session.query(Edge).all():
            sid = str(e.source_id)
            tid = str(e.target_id)
            if sid in visible_set and tid in visible_set:
                edges_out.append({
                    "id": str(e.id),
                    "source_id": sid,
                    "target_id": tid,
                    "relationship_type": e.relationship_type or "",
                    "sentence": e.sentence or "",
                    "importance": float(e.importance or 0.5),
                    "confidence": float(e.confidence or 0.5),
                })

    return jsonify({
        "nodes": nodes_out,
        "edges": edges_out,
        "bridge_edges": [],
        "seeds": [seed_id],
        "time_mode": "current",
        "time_from": None,
        "time_to": None,
        "total_candidates": len(candidate_entity_ids),
        "timestamp": to_rfc3339_z(utc_now()),
    })


@me_api.route("/api/me/seed-graph", methods=["GET"])
def seed_graph():
    """Bounded subgraph computed from personalized PageRank.

    Query params:
      seeds       comma-separated node ids. Required (or empty for default).
      limit       max nodes to return (default 50, max 100).
      time_mode   "current" | "lifetime" | "range".
      time_from   ISO date (only used when time_mode=range).
      time_to     ISO date (only used when time_mode=range).
    """
    seeds = _parse_csv_param("seeds")
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, HARD_LIMIT))

    time_mode = (request.args.get("time_mode") or "current").strip().lower()
    if time_mode not in ("current", "lifetime", "range"):
        time_mode = "current"

    time_from = (request.args.get("time_from") or "").strip() or None
    time_to = (request.args.get("time_to") or "").strip() or None

    include_concepts = (
        str(request.args.get("include_concepts") or "").strip().lower()
        in ("1", "true", "yes")
    )

    categories_param = (request.args.get("categories") or "").strip()
    categories: Optional[List[str]] = None
    if categories_param:
        categories = [c.strip() for c in categories_param.split(",") if c.strip()]

    # Empty seeds → fall back to default (the user's node).
    if not seeds:
        default_id = _resolve_default_seed_id()
        if default_id:
            seeds = [default_id]

    result = compute_seed_graph(
        seed_ids=seeds,
        limit=limit,
        time_mode=time_mode,
        time_from=time_from,
        time_to=time_to,
        include_concepts=include_concepts,
        categories=categories,
    )

    return jsonify({
        "nodes": result.nodes,
        "edges": result.edges,
        "bridge_edges": result.bridge_edges,
        "seeds": result.seeds,
        "time_mode": result.time_mode,
        "time_from": result.time_from,
        "time_to": result.time_to,
        "total_candidates": result.total_candidates,
        "timestamp": to_rfc3339_z(utc_now()),
    })


@me_api.route("/api/me/node/<node_id>", methods=["GET"])
def node_detail(node_id: str):
    """Single node details for the wiki side panel / quick-look popover."""
    with get_db_manager().read_session() as session:
        node = session.query(Node).filter(Node.id == node_id).first()
        if not node:
            return jsonify({"error": "node not found"}), 404

        in_count = session.query(Edge).filter(Edge.target_id == node_id).count()
        out_count = session.query(Edge).filter(Edge.source_id == node_id).count()

        is_goal = (node.node_type or "") == "Goal"
        # goal_status + last_pursued_at: both first-class columns now
        # (last_pursued_at promoted from attributes JSON 2026-05-11).
        return jsonify({
            "id": str(node.id),
            "label": node.label or "",
            "node_type": str(node.node_type or ""),
            "category": node.category,
            "aliases": list(node.aliases or []),
            "description": node.description or "",
            "original_sentence": node.original_sentence or "",
            "start_date": node.start_date.isoformat() if node.start_date else None,
            "end_date": node.end_date.isoformat() if node.end_date else None,
            "importance": float(node.importance or 0.5),
            "confidence": float(node.confidence or 0.5),
            "edges_in": in_count,
            "edges_out": out_count,
            "wiki_url": _wiki_url_for(node),
            # Goal lifecycle fields. None for non-Goal node types.
            "goal_status": (node.goal_status if is_goal else None),
            "last_pursued_at": (
                node.last_pursued_at.isoformat()
                if is_goal and node.last_pursued_at else None
            ),
        })


@me_api.route("/api/me/parse-query", methods=["POST"])
def parse_query():
    """Run me_query_filter_manager on user chat input.

    The manager runs me::query_planner (Planner with kg_query SQL access),
    which executes one or more SQL queries to resolve named entities, then
    me::query_final emits the structured filter intent.

    Body:
      { "text": str,
        "current_seeds": [node_id, ...],
        "visible_nodes": [{"id": str, "label": str, "node_type": str}, ...] }

    Returns the structured intent. Frontend applies it to its state.
    """
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    current_seeds = body.get("current_seeds") or []
    visible_nodes = body.get("visible_nodes") or []

    if not text:
        return jsonify({
            "intent": "noop",
            "seed_node_ids": [],
            "message": "",
        })

    # Deterministic shortcuts — don't pay for an LLM call on these.
    lowered = text.lower().strip()
    if lowered in ("reset", "clear", "start over"):
        return jsonify({
            "intent": "reset",
            "seed_node_ids": [],
            "message": "Resetting to default view.",
        })
    if lowered in ("lifetime", "show all time", "all time"):
        return jsonify({
            "intent": "set_time_mode",
            "time_mode": "lifetime",
            "seed_node_ids": [],
            "message": "Showing all time.",
        })
    if lowered in ("current", "currently true", "now"):
        return jsonify({
            "intent": "set_time_mode",
            "time_mode": "current",
            "seed_node_ids": [],
            "message": "Showing currently-true.",
        })

    # Build the lens-state context the planner gets in `information`.
    catalog_lines: List[str] = ["## Currently visible nodes (id  label  type):"]
    for n in visible_nodes[:80]:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        lbl = str(n.get("label") or "").strip()
        nt = str(n.get("node_type") or "").strip()
        if not nid or not lbl:
            continue
        catalog_lines.append(f"- {nid}  {lbl}  ({nt})")
    if current_seeds:
        catalog_lines.append("")
        catalog_lines.append(f"## Current seeds: {', '.join(current_seeds)}")
    information_text = "\n".join(catalog_lines)

    # Invoke me_query_filter_manager via ManagerInterface — the same path
    # other manager-tools use. The planner's kg_query loop resolves names
    # to ids; me::query_final emits the structured intent.
    try:
        from app.assistant.lib.core_tools.manager_interface.manager_interface import (
            ManagerInterface,
        )
        from app.assistant.utils.pydantic_classes import (
            ScopeApprovalPolicy,
            ScopeContext,
            ScopeResourcePolicy,
            ToolMessage,
        )
        import uuid

        scope = ScopeContext(
            scope_id="scope::me::query_filter",
            owner_id="jukka",
            actor_id="me_lens",
            surface="ui",
            room_id="me_lens",
            approval=ScopeApprovalPolicy(authority_level=99),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        )

        tool_message = ToolMessage(
            request_id=f"me_parse_{uuid.uuid4().hex[:8]}",
            tool_name="me_query_filter_manager",
            tool_data={
                "arguments": {
                    "task": text,
                    "information": information_text,
                },
            },
            scope_context=scope,
        )

        interface = ManagerInterface("me_query_filter_manager")
        result = interface.execute(tool_message)
        data = getattr(result, "data", None) or {}

        # me::query_final packs a JSON payload into final_answer_answer.
        # Parse it; fall back to a noop if the JSON is malformed.
        import json as _json
        payload_text = str(data.get("final_answer_answer") or "").strip()
        intent = "noop"
        seed_ids: List[str] = []
        time_mode_v = None
        time_from_v = None
        time_to_v = None
        message_v = str(result.content or "").strip()
        try:
            if payload_text.startswith("{"):
                payload = _json.loads(payload_text)
                if isinstance(payload, dict):
                    intent = str(payload.get("intent") or "noop")
                    seed_ids = [
                        str(s) for s in (payload.get("seed_node_ids") or [])
                        if isinstance(s, str)
                    ]
                    time_mode_v = payload.get("time_mode")
                    time_from_v = payload.get("time_from")
                    time_to_v = payload.get("time_to")
                    message_v = str(payload.get("message") or message_v)
        except Exception as parse_err:
            logger.warning("parse-query: JSON decode failed: %s", parse_err)

        return jsonify({
            "intent": intent,
            "seed_node_ids": seed_ids,
            "time_mode": time_mode_v,
            "time_from": time_from_v,
            "time_to": time_to_v,
            "message": message_v,
        })
    except Exception as e:
        logger.error("parse_query manager invocation failed: %s", e, exc_info=True)
        return jsonify({
            "intent": "noop",
            "seed_node_ids": [],
            "message": f"sorry — couldn't parse that: {e}",
        }), 200


@me_api.route("/api/me/default-seed", methods=["GET"])
def default_seed():
    """Empty-state seed = the user's own node. Used by the frontend on
    first load to know what to focus on."""
    nid = _resolve_default_seed_id()
    if nid is None:
        return jsonify({"error": "default seed not found"}), 404
    return jsonify({"node_id": nid})


# ---------- helpers ----------


def _resolve_default_seed_id() -> Optional[str]:
    """The user's own node — typically `Jukka` (Person, Entity).

    Look up by label among Entity-type nodes. Falls back to the highest-
    importance Entity if exact match fails.
    """
    from app.assistant.utils.identity_names import get_required_primary_user_name

    try:
        user_first = get_required_primary_user_name()
    except Exception:
        user_first = "Jukka"

    with get_db_manager().read_session() as session:
        node = (
            session.query(Node)
            .filter(Node.label == user_first, Node.node_type == "Entity")
            .first()
        )
        if node is not None:
            return str(node.id)

        # Fallback: highest-importance Entity.
        node = (
            session.query(Node)
            .filter(Node.node_type == "Entity")
            .order_by(Node.importance.desc().nullslast())
            .first()
        )
        if node is not None:
            logger.warning(
                "default_seed: '%s' not found, falling back to highest-importance Entity '%s'",
                user_first, node.label,
            )
            return str(node.id)

    return None


def _wiki_url_for(node: Node) -> Optional[str]:
    """The wiki page URL for this node, or None if no page exists.

    The wiki_generator writes per-entity pages to a vault directory; the
    Flask /wiki blueprint serves them by entity label. We don't check
    existence on disk here — the frontend's side panel handles 404.
    """
    if not node or not node.label:
        return None
    if (node.node_type or "") != "Entity":
        return None
    # URL-encode the label; the wiki blueprint canonicalizes server-side.
    from urllib.parse import quote
    return f"/wiki/{quote(node.label)}"
