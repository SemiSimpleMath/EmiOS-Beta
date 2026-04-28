"""
/kg-interests — Editor for the user-configurable KG extraction interests.

This resource (resource_kg_interests.json) controls which categories of
content the window_critic considers worth extracting into the knowledge
graph. Users can add, remove, or rephrase categories to shape what lands
in the graph without ever touching a prompt file.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

kg_interests_bp = Blueprint("kg_interests", __name__)

RESOURCE_ID = "resource_kg_interests"


def _resource_path() -> Path:
    # Lives alongside other user resources.
    return Path(__file__).resolve().parents[2] / "resources" / "user" / f"{RESOURCE_ID}.json"


def _load_resource() -> dict:
    path = _resource_path()
    if not path.exists():
        return {"description": "", "categories": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to parse %s: %s", path, e)
        return {"description": "", "categories": []}


@kg_interests_bp.route("/kg-interests", methods=["GET"])
def kg_interests_view():
    data = _load_resource()
    return render_template(
        "kg_interests.html",
        description=data.get("description", ""),
        categories=data.get("categories", []) or [],
    )


@kg_interests_bp.route("/kg-interests", methods=["POST"])
def kg_interests_save():
    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, list):
        return jsonify({"ok": False, "error": "categories must be a list of strings"}), 400
    categories = [str(c).strip() for c in raw_categories if str(c).strip()]
    if not categories:
        return jsonify({"ok": False, "error": "categories list cannot be empty"}), 400

    new_value = {"description": description, "categories": categories}

    try:
        DI.resource_manager.update_resource(RESOURCE_ID, new_value, persist=True)
    except Exception as e:
        logger.error("Failed to update %s: %s", RESOURCE_ID, e)
        return jsonify({"ok": False, "error": f"update failed: {e}"}), 500

    return jsonify({"ok": True, "categories": categories, "description": description})
