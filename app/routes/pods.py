"""Owner-only /pods management page + API.

A local-only surface for browsing every pod in pod_store and editing the two
curation fields the user controls: ``importance`` (0-10 priority signal) and
``min_authority`` (read-permission band). Distinct from the chat-facing
``/api/pods/<id>`` route in pod_api.py, which is fail-closed to displayable
research_finding pods for inline chat rendering; THIS surface is gated by
``reject_if_not_local`` (the owner's local browser) and shows ALL kinds with
full bodies — so it never goes on the public/proxied surface.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.assistant.pod_store.authority import (
    AUTH_CHAT,
    AUTH_COURIER,
    AUTH_GATED,
    AUTH_PUBLIC,
    AUTH_USER,
)
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.identity_names import get_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.routes._security import reject_if_not_local

logger = get_logger(__name__)

pods_bp = Blueprint("pods", __name__)
# Owner-only: this surface serves every pod kind with full bodies + lets the
# user re-band read-permission. It must never answer a proxied/tunneled request.
pods_bp.before_request(reject_if_not_local)

# Named authority bands surfaced in the permission dropdown (see
# app/assistant/pod_store/authority.py for the policy).
AUTHORITY_BANDS = [
    {"value": AUTH_PUBLIC, "label": "10 · public — any agent"},
    {"value": AUTH_CHAT, "label": "50 · chat surface — default"},
    {"value": AUTH_GATED, "label": "70 · gated chat — sensitive"},
    {"value": AUTH_USER, "label": "99 · user-equivalent — owner confirm"},
    {"value": AUTH_COURIER, "label": "100 · courier-only — no LLM"},
]

_LIST_LIMIT = 2000
_PREVIEW_CHARS = 240


@pods_bp.route("/pods")
def pods_page():
    return render_template(
        "pods.html",
        assistant_name=get_assistant_name(),
        authority_bands=AUTHORITY_BANDS,
    )


def _header(pod) -> dict:
    body = pod.body or ""
    return {
        "pod_id": pod.pod_id,
        "kind": pod.kind,
        "one_liner": pod.one_liner or "",
        "preview": body[:_PREVIEW_CHARS],
        "has_more": len(body) > _PREVIEW_CHARS,
        "importance": pod.importance,
        "min_authority": pod.min_authority,
        "scope_id": pod.scope_id,
        "tags": list(pod.tags or []),
        "created_by": pod.created_by,
        "created_at": pod.created_at.isoformat() if pod.created_at else None,
    }


@pods_bp.route("/api/pods/manage/list")
def pods_list():
    """All pods, newest-first. Filtering/sorting happens client-side so the
    page stays responsive; the body is truncated to a preview here and the full
    body is fetched on demand via /item."""
    pods = PodStore().query(limit=_LIST_LIMIT)
    return jsonify(
        {
            "pods": [_header(p) for p in pods],
            "count": len(pods),
            "capped": len(pods) >= _LIST_LIMIT,
        }
    )


@pods_bp.route("/api/pods/manage/item")
def pods_item():
    """One pod's full contents (any kind). Owner-only via the blueprint gate."""
    pod_id = (request.args.get("pod_id") or "").strip()
    if not pod_id:
        return jsonify({"error": "pod_id required"}), 400
    pod = PodStore().get(pod_id)
    if pod is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(
        {
            "pod_id": pod.pod_id,
            "kind": pod.kind,
            "one_liner": pod.one_liner or "",
            "body": pod.body or "",
            "importance": pod.importance,
            "min_authority": pod.min_authority,
            "scope_id": pod.scope_id,
            "tags": list(pod.tags or []),
            "for_agents": list(pod.for_agents or []),
            "source_refs": [sr.model_dump() for sr in pod.source_refs],
            "metadata": pod.metadata or {},
            "created_by": pod.created_by,
            "created_at": pod.created_at.isoformat() if pod.created_at else None,
        }
    )


@pods_bp.route("/api/pods/manage/update", methods=["POST"])
def pods_update():
    """Set importance and/or min_authority on a pod. Body content is immutable.

    JSON body: ``{pod_id, importance?, min_authority?}``. ``importance`` may be a
    number 0-10 or null (unrated); an omitted key leaves that field unchanged.
    """
    data = request.get_json(silent=True) or {}
    pod_id = (data.get("pod_id") or "").strip()
    if not pod_id:
        return jsonify({"error": "pod_id required"}), 400

    kwargs = {}
    if "importance" in data:
        imp = data["importance"]
        if imp is None or imp == "":
            kwargs["importance"] = None
        else:
            try:
                impf = float(imp)
            except (TypeError, ValueError):
                return jsonify({"error": "importance must be a number 0-10 or null"}), 400
            if not (0.0 <= impf <= 10.0):
                return jsonify({"error": "importance must be between 0 and 10"}), 400
            kwargs["importance"] = impf
    if "min_authority" in data:
        try:
            ma = int(data["min_authority"])
        except (TypeError, ValueError):
            return jsonify({"error": "min_authority must be an integer"}), 400
        if not (0 <= ma <= 100):
            return jsonify({"error": "min_authority must be between 0 and 100"}), 400
        kwargs["min_authority"] = ma

    if not kwargs:
        return jsonify({"error": "nothing to update — pass importance and/or min_authority"}), 400

    try:
        PodStore().update_curation(pod_id, **kwargs)
    except KeyError:
        return jsonify({"error": "not found"}), 404

    pod = PodStore().get(pod_id)
    return jsonify(
        {"ok": True, "importance": pod.importance, "min_authority": pod.min_authority}
    )
