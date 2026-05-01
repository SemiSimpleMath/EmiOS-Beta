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

    include_concepts = (
        str(request.args.get("include_concepts") or "").strip().lower()
        in ("1", "true", "yes")
    )

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
