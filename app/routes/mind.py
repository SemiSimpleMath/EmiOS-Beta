"""Owner-only /mind page: how the brain thinks, told in plain words (2026-10-02,
docs/design/brain_insight_2026-10-02.md).

What reached the brain on a day and what became of each piece, the story of every open concern and where
it is going, written by the story agents (subconscious/brain_stories.py) and read here as stored. Opening
the page makes no model call. Each concern links to /brain/flow, filtered to the wakes that touched it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app.routes._security import reject_if_not_local

mind_bp = Blueprint("mind", __name__)
mind_bp.before_request(reject_if_not_local)


@mind_bp.route("/mind")
def mind_page():
    return render_template("mind.html")


@mind_bp.route("/api/mind")
def mind_api():
    """One local day (day=YYYY-MM-DD; today when omitted)."""
    from app.assistant.subconscious import brain_stories
    now = datetime.now(timezone.utc)
    day = (request.args.get("day") or "").strip() or brain_stories._window_days(now)[-1]
    datetime.fromisoformat(day)
    return jsonify(brain_stories.day_view(day, now_utc=now))
