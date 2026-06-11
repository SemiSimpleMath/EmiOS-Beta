"""REST blueprint for KG Lens (`/kg-lens`).

KG Lens = the graph visualizer + an editable node sidebar. The page reuses
the generic subgraph services from the me-lens blueprint (/api/me/seed-graph,
/api/me/parse-query, /api/me/default-seed — they are KG-subgraph services in
all but name); this blueprint adds what is new:

- GET  /api/kg-lens/node/<id>        rich node detail for the sidebar
- POST /api/kg-lens/node/<id>/edit   single-field edit via the audited mutator
- POST /api/kg-lens/node/<id>/lock   user lock/unlock (axiom layer) via mutator

All writes go through KGMutatorTool so every change lands in kg_revision_log
with before/after snapshots (reversible), and locked rows refuse edits.
Write endpoints are local-only: this is the user's own curation surface.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_core.kg_utils.date_display import format_node_date
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager
from app.routes._security import is_local_request

logger = get_logger(__name__)

kg_lens_api = Blueprint("kg_lens_api", __name__)

# Fields the sidebar may edit, mapped to the mutator op that applies them.
# `label` goes through kg_rename_label (alias-preserving); everything else
# through kg_update_node_field. Date fields additionally stamp the matching
# confidence to user_set — a user-typed date is a user-set date.
_FIELD_EDIT_WHITELIST = {
    "label",
    "original_sentence",
    "description",
    "category",
    "semantic_label",
    "valid_during",
    "start_date",
    "end_date",
    "start_date_prose",
    "end_date_prose",
    "importance",
    "goal_status",
}

_EDGE_LIST_CAP = 80
_REVISIONS_CAP = 10


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _edge_row(edge: Edge, other: Optional[Node], *, other_id: str) -> Dict[str, Any]:
    return {
        "edge_id": str(edge.id),
        "rel": edge.relationship_type or "",
        "sentence": edge.sentence or "",
        "other_id": other_id,
        "other_label": (other.label or "") if other is not None else "(missing)",
        "other_type": str(other.node_type or "") if other is not None else "",
    }


@kg_lens_api.route("/api/kg-lens/focus-graph", methods=["GET"])
def focus_graph():
    """Full positioned 3D neighborhood of one or more focus nodes.

    Query params:
      node_ids    comma-separated focus node ids (up to 3). With several,
                  the result is the INTERSECTION — nodes attached to every
                  focus. `node_id` (singular) accepted as an alias; empty
                  falls back to the default seed (the user's own node).
      mode        'auto' (default: intersection when multiple) |
                  'intersection' | 'union'.
      max_nodes   importance-ranked cap (default 400, max 1500).

    Positions are deterministic per focus set — the client filters by
    importance threshold (ctrl+scroll) without any re-layout.
    """
    from app.kg_lens.focus_graph import compute_focus_graph, DEFAULT_MAX_NODES

    raw = (request.args.get("node_ids") or request.args.get("node_id") or "").strip()
    node_ids = [s.strip() for s in raw.split(",") if s.strip()]
    if not node_ids:
        from app.me.api import _resolve_default_seed_id
        default_id = _resolve_default_seed_id() or ""
        if not default_id:
            return jsonify({"error": "no focus node"}), 400
        node_ids = [default_id]

    try:
        max_nodes = int(request.args.get("max_nodes", DEFAULT_MAX_NODES))
    except (TypeError, ValueError):
        max_nodes = DEFAULT_MAX_NODES

    payload = compute_focus_graph(
        node_ids,
        max_nodes=max_nodes,
        mode=(request.args.get("mode") or "auto"),
    )
    if payload is None:
        return jsonify({"error": "focus node not found"}), 404
    return jsonify(payload)


@kg_lens_api.route("/api/kg-lens/set-graph", methods=["POST"])
def set_graph():
    """Frozen layout for an explicit node set (agent show_nodes intent).

    Body: {node_ids: [uuid, ...], max_nodes?}. Returns the same payload
    shape as focus-graph with mode='explicit_set'.
    """
    from app.kg_lens.focus_graph import compute_set_graph, DEFAULT_MAX_NODES

    body = request.get_json(silent=True) or {}
    node_ids = [str(s) for s in (body.get("node_ids") or []) if isinstance(s, str)]
    if not node_ids:
        return jsonify({"error": "node_ids is required"}), 400
    try:
        max_nodes = int(body.get("max_nodes", DEFAULT_MAX_NODES))
    except (TypeError, ValueError):
        max_nodes = DEFAULT_MAX_NODES

    return jsonify(compute_set_graph(node_ids, max_nodes=max_nodes))


@kg_lens_api.route("/api/kg-lens/node/<node_id>", methods=["GET"])
def node_detail(node_id: str):
    """Everything the sidebar needs about one node, in one call."""
    with get_db_manager().read_session() as session:
        node = session.query(Node).filter(Node.id == node_id).first()
        if node is None:
            return jsonify({"error": "node not found"}), 404

        edges_out_q = (
            session.query(Edge)
            .filter(Edge.source_id == node_id)
            .order_by(Edge.updated_at.desc().nullslast())
            .limit(_EDGE_LIST_CAP)
            .all()
        )
        edges_in_q = (
            session.query(Edge)
            .filter(Edge.target_id == node_id)
            .order_by(Edge.updated_at.desc().nullslast())
            .limit(_EDGE_LIST_CAP)
            .all()
        )

        # Resolve counterpart labels in one query.
        other_ids = {str(e.target_id) for e in edges_out_q} | {str(e.source_id) for e in edges_in_q}
        others: Dict[str, Node] = {}
        if other_ids:
            for n in session.query(Node).filter(Node.id.in_(other_ids)).all():
                others[str(n.id)] = n

        edges_out = [
            _edge_row(e, others.get(str(e.target_id)), other_id=str(e.target_id))
            for e in edges_out_q
        ]
        edges_in = [
            _edge_row(e, others.get(str(e.source_id)), other_id=str(e.source_id))
            for e in edges_in_q
        ]

        from app.assistant.database.kg_revision_log import KGRevisionLog
        from sqlalchemy import cast, String

        revisions: List[Dict[str, Any]] = []
        rev_rows = (
            session.query(KGRevisionLog)
            .filter(cast(KGRevisionLog.args_json, String).like(f"%{node_id}%"))
            .order_by(KGRevisionLog.created_at.desc())
            .limit(_REVISIONS_CAP)
            .all()
        )
        for r in rev_rows:
            revisions.append({
                "op": r.op,
                "reason": r.reason or "",
                "created_at": _iso(r.created_at),
                "agent_id": r.agent_id,
            })

        from sqlalchemy import text as _sql_text
        evidence_count = session.execute(
            _sql_text("SELECT COUNT(1) FROM kg_node_evidence WHERE node_id = :nid"),
            {"nid": node_id},
        ).scalar() or 0

        is_goal = (node.node_type or "") == "Goal"
        from app.me.api import _wiki_url_for

        return jsonify({
            "id": str(node.id),
            "label": node.label or "",
            "node_type": str(node.node_type or ""),
            "category": node.category,
            "semantic_label": node.semantic_label,
            "aliases": list(node.aliases or []),
            "description": node.description or "",
            "original_sentence": node.original_sentence or "",
            "start_date": _iso(node.start_date),
            "end_date": _iso(node.end_date),
            "start_date_prose": node.start_date_prose or "",
            "end_date_prose": node.end_date_prose or "",
            "start_date_confidence": node.start_date_confidence,
            "end_date_confidence": node.end_date_confidence,
            "display_start": format_node_date(node.start_date, node.start_date_confidence),
            "display_end": format_node_date(node.end_date, node.end_date_confidence),
            "valid_during": getattr(node, "valid_during", None),
            "valid_currently": getattr(node, "valid_currently", None),
            "goal_status": (node.goal_status if is_goal else None),
            "last_pursued_at": _iso(node.last_pursued_at) if is_goal else None,
            "importance": float(node.importance) if node.importance is not None else None,
            "confidence": float(node.confidence) if node.confidence is not None else None,
            "confidence_tier": getattr(node, "confidence_tier", None),
            "locked": node.locked_by_user_at is not None,
            "locked_at": _iso(node.locked_by_user_at),
            "created_at": _iso(node.created_at),
            "updated_at": _iso(node.updated_at),
            "wiki_url": _wiki_url_for(node),
            "evidence_count": int(evidence_count),
            "edges_out": edges_out,
            "edges_in": edges_in,
            "revisions": revisions,
        })


def _run_mutator(tool_name: str, arguments: Dict[str, Any]):
    """One audited mutator call. Returns (ok, content, data)."""
    from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
    from app.assistant.utils.pydantic_classes import ToolMessage

    msg = ToolMessage(
        tool_name=tool_name,
        tool_data={"tool_name": tool_name, "arguments": arguments},
    )
    result = KGMutatorTool().execute(msg)
    data = result.data if isinstance(result.data, dict) else {}
    ok = bool(data.get("ok"))
    return ok, str(result.content or ""), data


@kg_lens_api.route("/api/kg-lens/node/<node_id>/edit", methods=["POST"])
def node_edit(node_id: str):
    """Single-field edit from the sidebar, through the audited mutator.

    Body: {field, value, reason}. A date edit also stamps the matching
    confidence to user_set in the same request — a user-typed date IS a
    user-set date (the year-floor doctrine's authoritative tier).
    """
    if not is_local_request():
        return jsonify({"ok": False, "error": "local requests only"}), 403

    body = request.get_json(silent=True) or {}
    field = str(body.get("field") or "").strip()
    reason = str(body.get("reason") or "").strip()
    value = body.get("value")

    if field not in _FIELD_EDIT_WHITELIST:
        return jsonify({"ok": False, "error": f"field {field!r} is not editable"}), 400
    if not reason:
        return jsonify({"ok": False, "error": "a non-empty reason is required"}), 400

    lens_reason = f"[kg_lens user edit] {reason}"

    if field == "label":
        new_label = str(value or "").strip()
        if not new_label:
            return jsonify({"ok": False, "error": "label cannot be empty"}), 400
        ok, content, data = _run_mutator("kg_rename_label", {
            "node_id": node_id,
            "new_label": new_label,
            "reason": lens_reason,
            "dry_run": False,
        })
    else:
        ok, content, data = _run_mutator("kg_update_node_field", {
            "node_id": node_id,
            "field": field,
            "value": value if value is not None else "",
            "list_op": "",
            "reason": lens_reason,
            "dry_run": False,
        })
        # Date edits carry user authority: stamp the confidence alongside.
        if ok and field in ("start_date", "end_date") and str(value or "").strip():
            conf_field = f"{field}_confidence"
            conf_ok, conf_content, _ = _run_mutator("kg_update_node_field", {
                "node_id": node_id,
                "field": conf_field,
                "value": "user_set",
                "list_op": "",
                "reason": lens_reason,
                "dry_run": False,
            })
            if not conf_ok:
                logger.error("kg_lens edit: %s stamp failed: %s", conf_field, conf_content)
                content += f" (warning: {conf_field} stamp failed: {conf_content})"

    status = 200 if ok else 400
    return jsonify({"ok": ok, "detail": content, "error": None if ok else content}), status


@kg_lens_api.route("/api/kg-lens/node/<node_id>/lock", methods=["POST"])
def node_lock(node_id: str):
    """Lock (axiom) or unlock a node. Body: {locked, reason}."""
    if not is_local_request():
        return jsonify({"ok": False, "error": "local requests only"}), 403

    body = request.get_json(silent=True) or {}
    if "locked" not in body:
        return jsonify({"ok": False, "error": "`locked` (bool) is required"}), 400
    locked = bool(body.get("locked"))
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "a non-empty reason is required"}), 400

    ok, content, data = _run_mutator("kg_set_user_lock", {
        "node_id": node_id,
        "locked": locked,
        "reason": f"[kg_lens user lock] {reason}",
        "dry_run": False,
    })
    status = 200 if ok else 400
    return jsonify({"ok": ok, "detail": content, "error": None if ok else content}), status
