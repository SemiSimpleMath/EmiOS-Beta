"""/subconscious — dashboard for subconscious suggestions + universal
comment-submission endpoint.

GET /subconscious renders this week's intention.* pods grouped by date
with comment boxes per item and inline display of past comments.

POST /subconscious/comment is the universal comment endpoint — every
surface (this dashboard + /meals + future per-domain pages) POSTs here
with target_pod_id + text. Mints a feedback.comment pod for downstream
belief extraction.
"""
from flask import Blueprint, jsonify, render_template, request

from app.assistant.subconscious.feedback_service import (
    fetch_comments_for,
    list_intentions_for_dashboard,
    mint_feedback_comment,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

subconscious_bp = Blueprint("subconscious", __name__)


@subconscious_bp.route("/subconscious")
def subconscious_page():
    rows = list_intentions_for_dashboard(days_ahead=14)
    # Group by date for clean rendering — the template iterates date buckets.
    by_date: dict = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)
    grouped = [
        {"date": d, "items": by_date[d]}
        for d in sorted(by_date.keys())
    ]
    return render_template(
        "subconscious.html",
        grouped=grouped,
        item_count=len(rows),
    )


@subconscious_bp.route("/subconscious/comment", methods=["POST"])
def submit_comment():
    """Universal comment endpoint.

    Body (JSON or form): {target_pod_id, text, target_scope?}.

    Returns JSON: {status: 'ok'|'error', pod_id?, comments?, message?}.
    Comments list lets the caller refresh the UI block without a full
    page reload."""
    payload = request.get_json(silent=True) or request.form or {}
    target_pod_id = str(payload.get("target_pod_id") or "").strip()
    text = str(payload.get("text") or "").strip()
    target_scope = (payload.get("target_scope") or None)

    if not target_pod_id:
        return jsonify({"status": "error", "message": "target_pod_id required"}), 400
    if not text:
        return jsonify({"status": "error", "message": "text required"}), 400

    pod_id = mint_feedback_comment(
        target_pod_id=target_pod_id,
        text=text,
        target_scope=target_scope,
    )
    if not pod_id:
        return jsonify({"status": "error", "message": "failed to mint comment"}), 500

    comments = fetch_comments_for(target_pod_id, limit=10)
    return jsonify({"status": "ok", "pod_id": pod_id, "comments": comments}), 200
