"""/beliefs-shadow — the belief v1-vs-v2 shadow-run comparison page.

Both belief engines run in parallel (v2 feeds the meal agents; v1 keeps
operating underneath). Every meal-agent run records what BOTH lanes
produced; this page renders the diffs plus the v2 engine's recent
activity (observations folded, beliefs minted/reinforced, resolver mix,
contested beliefs, merges) so decay/resolver tuning and the eventual v1
retirement rest on evidence.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

beliefs_shadow_bp = Blueprint("beliefs_shadow", __name__)


@beliefs_shadow_bp.route("/beliefs-shadow")
def beliefs_shadow_page():
    from belief_engine_v2.shadow import diff_run, load_shadow_runs, v2_activity

    try:
        days = float(request.args.get("days", 7))
    except (TypeError, ValueError):
        days = 7.0

    runs = load_shadow_runs(days=days)
    diffs = []
    for run in runs[::-1]:  # newest first
        d = diff_run(run)
        d["v2_items"] = run.get("v2") or []
        d["v1_items"] = run.get("v1_items") or []
        diffs.append(d)

    activity = None
    activity_error = None
    try:
        activity = v2_activity(days=days)
    except Exception as e:
        logger.error("[beliefs_shadow] v2 activity failed: %s", e)
        activity_error = str(e)

    return render_template(
        "beliefs_shadow.html",
        days=days,
        diffs=diffs,
        activity=activity,
        activity_error=activity_error,
    )
