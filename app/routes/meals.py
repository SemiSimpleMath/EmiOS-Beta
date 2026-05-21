"""/meals — surface for the meal subconscious.

GET /meals[?week=YYYY-MM-DD] renders the weekly meal plan for the
requested week (defaults to the latest plan if any, else this week's
Monday), with prev/next-week navigation chrome.

POST /meals/send-to-katy emails the currently-displayed plan to Katy.

POST /meals/generate runs the weekly meal planning chain for the given
week_start and persists the plan pod. Used by the empty-state "Generate
plan for this week" button.

View-model assembly + send + audit + chain invocation all live in
app/assistant/subconscious/meal_page_service.py — the route stays a
thin wrapper.
"""
from flask import Blueprint, jsonify, render_template, request

from app.assistant.subconscious.meal_page_service import (
    build_page_view_model,
    generate_plan_for_week,
    send_meal_plan_email,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

meals_bp = Blueprint("meals", __name__)


@meals_bp.route("/meals")
def meals_page():
    week_start = (request.args.get("week") or "").strip() or None
    view_model = build_page_view_model(week_start=week_start)
    return render_template("meals.html", **view_model)


@meals_bp.route("/meals/send-to-katy", methods=["POST"])
def send_to_katy():
    result = send_meal_plan_email()
    http_status = 200 if result.get("status") == "ok" else 400
    return jsonify(result), http_status


@meals_bp.route("/meals/generate", methods=["POST"])
def generate_for_week():
    payload = request.get_json(silent=True) or request.form or {}
    week_start = str(payload.get("week_start") or "").strip()
    if not week_start:
        return jsonify({"status": "error", "message": "week_start required"}), 400
    result = generate_plan_for_week(week_start)
    http_status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), http_status
