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

from datetime import datetime
from typing import Any, Dict, List, Optional

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
    )

    return jsonify({
        "nodes": result.nodes,
        "edges": result.edges,
        "seeds": result.seeds,
        "time_mode": result.time_mode,
        "time_from": result.time_from,
        "time_to": result.time_to,
        "total_candidates": result.total_candidates,
        "timestamp": datetime.utcnow().isoformat(),
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
        })


@me_api.route("/api/me/parse-query", methods=["POST"])
def parse_query():
    """Run the me::query_filter agent on user chat input.

    Body:
      { "text": str,
        "current_seeds": [node_id, ...],
        "visible_nodes": [{"id": str, "label": str, "node_type": str}, ...] }

    Returns the agent's structured intent. Frontend applies it to its state.
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

    # Deterministic shortcuts — don't burn an LLM call on these.
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

    # Build the agent context: catalog of visible nodes + current seeds.
    catalog_lines: List[str] = ["## Available nodes (id  label  type):"]
    for n in visible_nodes[:80]:  # cap to avoid blowing the prompt
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

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import (
            Message,
            ScopeApprovalPolicy,
            ScopeContext,
            ScopeResourcePolicy,
        )

        agent = DI.agent_factory.create_agent("me::query_filter")
        if agent is None:
            return jsonify({"error": "agent unavailable"}), 500

        scope = ScopeContext(
            scope_id="scope::me::query_filter",
            owner_id="jukka",
            actor_id="me_lens",
            surface="ui",
            room_id="me_lens",
            approval=ScopeApprovalPolicy(authority_level=99),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        )

        msg = Message(
            agent_input={
                "task": text,
                "information": information_text,
            },
            task=text,
            information=information_text,
            scope_context=scope,
        )
        result = agent.action_handler(msg)
        data = getattr(result, "data", None) or {}

        return jsonify({
            "intent": str(data.get("intent") or "noop"),
            "seed_node_ids": list(data.get("seed_node_ids") or []),
            "time_mode": data.get("time_mode"),
            "time_from": data.get("time_from"),
            "time_to": data.get("time_to"),
            "message": str(data.get("message") or ""),
        })
    except Exception as e:
        logger.error("parse_query agent invocation failed: %s", e, exc_info=True)
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
