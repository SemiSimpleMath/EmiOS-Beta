"""/meals — surface for the meal subconscious.

GET /meals[?week=YYYY-MM-DD] renders the weekly meal plan for the
requested week (defaults to the latest plan if any, else this week's
Monday), with prev/next-week navigation chrome.

POST /meals/generate runs the weekly meal planning chain for the given
week_start and persists the plan pod. Used by the empty-state "Generate
plan for this week" button.

View-model assembly + send + audit + chain invocation all live in
app/assistant/subconscious/meal_page_service.py — the route stays a
thin wrapper.
"""
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from app.assistant.subconscious.easy_meals import (
    add_easy_meal,
    remove_easy_meal,
    update_easy_meal,
)
from app.assistant.subconscious.grocery_inventory import manual_acquire, remove_item
from app.assistant.subconscious.meal_page_service import (
    build_easy_meals_view_model,
    build_inventory_view_model,
    build_page_view_model,
    generate_plan_for_week,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

meals_bp = Blueprint("meals", __name__)


@meals_bp.route("/meals")
def meals_page():
    week_start = (request.args.get("week") or "").strip() or None
    view_model = build_page_view_model(week_start=week_start)
    return render_template("meals.html", **view_model)


@meals_bp.route("/meals/generate", methods=["POST"])
def generate_for_week():
    payload = request.get_json(silent=True) or request.form or {}
    week_start = str(payload.get("week_start") or "").strip()
    if not week_start:
        return jsonify({"status": "error", "message": "week_start required"}), 400
    result = generate_plan_for_week(week_start)
    http_status = 200 if result.get("status") == "ok" else 500
    return jsonify(result), http_status


@meals_bp.route("/meals/inventory")
def inventory_page():
    """Editor for what's actually in the kitchen. The meal planners read this
    (use-soon items, what's on hand), so keeping it accurate directly shapes the
    plans — e.g. removing an item the household won't eat stops it being forced
    into meals as a 'use soon' ingredient."""
    return render_template("meal_inventory.html", **build_inventory_view_model())


@meals_bp.route("/meals/inventory/add", methods=["POST"])
def inventory_add():
    raw = (request.form.get("items") or "").strip()
    # Accept comma- and/or newline-separated names in one box.
    names = [n.strip() for line in raw.splitlines() for n in line.split(",") if n.strip()]
    if names:
        manual_acquire(names, source_note="meals_ui")
    return redirect(url_for("meals.inventory_page"))


@meals_bp.route("/meals/inventory/remove", methods=["POST"])
def inventory_remove():
    item_id = (request.form.get("item_id") or "").strip()
    if item_id:
        remove_item(item_id, reason="meals_ui")
    return redirect(url_for("meals.inventory_page"))


@meals_bp.route("/meals/easy-meals")
def easy_meals_page():
    """The household's go-to dishes with a cadence ('OK every N days') and derived
    'days since last'. The planners read this rotation so go-to meals come back on
    schedule and don't repeat too soon."""
    return render_template("easy_meals.html", **build_easy_meals_view_model())


@meals_bp.route("/meals/easy-meals/add", methods=["POST"])
def easy_meals_add():
    name = (request.form.get("name") or "").strip()
    max_days = _int_or(request.form.get("max_days"), 7)
    notes = (request.form.get("notes") or "").strip()
    if name:
        add_easy_meal(name, max_days, notes)
    return redirect(url_for("meals.easy_meals_page"))


@meals_bp.route("/meals/easy-meals/update", methods=["POST"])
def easy_meals_update():
    meal_id = (request.form.get("meal_id") or "").strip()
    if meal_id:
        update_easy_meal(
            meal_id,
            max_days=_int_or(request.form.get("max_days"), None),
            notes=request.form.get("notes"),
        )
    return redirect(url_for("meals.easy_meals_page"))


@meals_bp.route("/meals/easy-meals/remove", methods=["POST"])
def easy_meals_remove():
    meal_id = (request.form.get("meal_id") or "").strip()
    if meal_id:
        remove_easy_meal(meal_id)
    return redirect(url_for("meals.easy_meals_page"))


def _int_or(raw, default):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default
