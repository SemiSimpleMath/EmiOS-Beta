"""/meals — surface for the meal subconscious.

GET /meals renders the current weekly meal plan + consolidated shopping
list, with a button to email it to Katy.

POST /meals/send-to-katy invokes the meal_page_service to send the email
and mint a delivery.email audit pod, returning JSON for the page's
button-click handler.

The view model (pod loading, shopping consolidation, send + audit) lives
in app/assistant/subconscious/meal_page_service.py — the route stays a
thin wrapper.
"""
from flask import Blueprint, jsonify, render_template

from app.assistant.subconscious.meal_page_service import (
    build_page_view_model,
    send_meal_plan_email,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

meals_bp = Blueprint("meals", __name__)


@meals_bp.route("/meals")
def meals_page():
    view_model = build_page_view_model()
    return render_template("meals.html", **view_model)


@meals_bp.route("/meals/send-to-katy", methods=["POST"])
def send_to_katy():
    result = send_meal_plan_email()
    http_status = 200 if result.get("status") == "ok" else 400
    return jsonify(result), http_status
