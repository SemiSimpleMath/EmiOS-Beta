"""work_objects.ui — a local, read-only viewer for the WorkObject graph.

A Flask blueprint (mounted by the main app) that reads the LOCAL work.db + pod store, so you
can watch a work graph: the goal -> subtask tree, what remains (ready / blocked / waiting),
evidence + artifact outcomes, the event log, and the pods a node references.

Read-only and lazy: it never mutates the graph, and the store is opened on the first request
(importing this module is free, and no empty db is created at boot). The work.db it reads is
gitignored (private) — only this code ships.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

work_ui_bp = Blueprint("work_ui", __name__, template_folder="templates")

_WORK_DB = os.environ.get("WORK_DB", "work_objects/work.db")
_store = None

# Non-terminal nodes on the WORK spine (not knowledge/evidence) = "what remains to do".
_WORK_TYPES = {"goal", "plan", "subtask", "tool", "question", "verification"}


def _get_store():
    global _store
    if _store is None:
        # Prefer the LIVE dayflow work store (emi.db, shared singleton) so /work shows real work
        # objects; fall back to the local work.db dev file if the app isn't available.
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            _store = get_dayflow_work_store()
        except Exception:
            from work_objects.store import WorkStore
            _store = WorkStore(_WORK_DB)
    return _store


def _iso(dt):
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def _node_dict(n) -> dict:
    return {
        "id": n.id, "type": n.type, "title": n.title or "", "status": n.status,
        "parent_id": n.parent_id, "owner_agent": n.owner_agent, "pod_ref": n.pod_ref,
        "content": n.content or "", "side_effect": n.side_effect,
        "satisfied_when_kind": n.satisfied_when_kind,
        "wake_kind": n.wake_kind, "wake_at": _iso(n.wake_at),
        "created_at": _iso(n.created_at), "updated_at": _iso(n.updated_at),
        "is_terminal": n.is_terminal, "payload": n.payload or {},
    }


@work_ui_bp.route("/work")
def work_page():
    return render_template("work.html")


@work_ui_bp.route("/api/work")
def api_work_list():
    store = _get_store()
    out = []
    for s in store.list_work_objects():
        try:
            wo = store.load(s["id"])
            nodes = list(wo.nodes.values())
            s["total_nodes"] = len(nodes)
            s["done_nodes"] = sum(1 for n in nodes if n.is_terminal)
            s["remaining_nodes"] = sum(
                1 for n in nodes if not n.is_terminal and n.type in _WORK_TYPES)
            goal = wo.nodes.get(wo.goal_node_id or "")
            s["goal"] = (goal.content or goal.title) if goal else s["title"]
        except Exception as e:
            logger.debug("[work_ui] summary for %s failed: %s", s["id"], e)
        out.append(s)
    return jsonify({"work_objects": out, "db": _WORK_DB})


@work_ui_bp.route("/api/work/<work_id>")
def api_work_detail(work_id):
    store = _get_store()
    try:
        wo = store.load(work_id)
    except KeyError:
        return jsonify({"error": f"work_object {work_id!r} not found"}), 404
    ready = {n.id for n in wo.ready_nodes()}
    blocked = {n.id for n in wo.blocked_nodes()}
    nodes = []
    for n in wo.nodes.values():
        d = _node_dict(n)
        d["ready"] = n.id in ready
        d["blocked"] = n.id in blocked
        nodes.append(d)
    edges = [{"id": e.id, "src": e.src, "dst": e.dst, "relation": e.relation,
              "payload": e.payload or {}} for e in wo.edges]
    return jsonify({
        "id": wo.id, "title": wo.title, "status": wo.status,
        "goal_node_id": wo.goal_node_id, "constraints": wo.constraints or {},
        "created_at": _iso(wo.created_at), "updated_at": _iso(wo.updated_at),
        "nodes": nodes, "edges": edges,
    })


@work_ui_bp.route("/api/work/<work_id>/events")
def api_work_events(work_id):
    store = _get_store()
    try:
        return jsonify({"events": store.events(work_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 404


@work_ui_bp.route("/api/work-pod")
def api_pod():
    pod_id = (request.args.get("id") or "").strip()
    if not pod_id:
        return jsonify({"error": "missing ?id="}), 400
    try:
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(pod_id)
    except Exception as e:
        logger.debug("[work_ui] pod fetch %s failed: %s", pod_id, e)
        return jsonify({"error": str(e)}), 500
    if pod is None:
        return jsonify({"error": f"pod {pod_id!r} not found"}), 404
    srcs = []
    for r in (getattr(pod, "source_refs", None) or []):
        srcs.append(getattr(r, "url", None) or getattr(r, "ref", None) or str(r))
    # research_finding (and similar) pods stash their URLs in metadata.source_urls,
    # not the structured source_refs field — surface those too so the links show.
    meta = getattr(pod, "metadata", None) or {}
    for u in (meta.get("source_urls") or []):
        if u and u not in srcs:
            srcs.append(u)
    return jsonify({
        "pod_id": pod.pod_id, "kind": pod.kind, "one_liner": pod.one_liner or "",
        "body": pod.body or "", "tags": list(getattr(pod, "tags", []) or []),
        "source_refs": srcs, "importance": getattr(pod, "importance", None),
        "metadata": meta, "created_at": _iso(getattr(pod, "created_at", None)),
    })
