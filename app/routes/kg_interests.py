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


# ---------------------------------------------------------------------------
# /kg-interests/test — paste chat lines, see whether window_critic accepts them.
# Read-only: no DB writes. Lets Jukka verify the live critic prompt + the
# current resource_kg_interests categories agree on what's extraction-worthy.
# ---------------------------------------------------------------------------

WINDOW_CRITIC_AGENT_NAME = "knowledge_graph_add::window_critic"


@kg_interests_bp.route("/kg-interests/test", methods=["GET"])
def kg_interests_test_view():
    return render_template("kg_interests_test.html")


@kg_interests_bp.route("/kg-interests/test", methods=["POST"])
def kg_interests_test_run():
    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("user_text") or "").strip()
    context_text = str(payload.get("context_text") or "").strip()

    if not user_text:
        return jsonify({"ok": False, "error": "user_text is required"}), 400

    from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
    from app.assistant.utils.pydantic_classes import Message

    user_block = user_text
    context_block = context_text or "(no assistant context)"

    try:
        agent = DI.agent_factory.create_agent(WINDOW_CRITIC_AGENT_NAME)
    except Exception as e:
        logger.error("Failed to create window_critic agent: %s", e)
        return jsonify({"ok": False, "error": f"agent_factory failed: {e}"}), 500
    if agent is None:
        return jsonify({"ok": False, "error": "window_critic agent unavailable"}), 500

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="window_critic_tester",
    )
    agent_input = {"context_lines": context_block, "user_lines": user_block}

    try:
        result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
    except Exception as e:
        logger.error("window_critic call failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": f"critic call failed: {e}"}), 500

    data = result.data if result and hasattr(result, "data") else {}
    if not isinstance(data, dict):
        data = {}
    extractable = bool(data.get("extractable", True))
    reason = str(data.get("reason") or "")[:1000]

    return jsonify({
        "ok": True,
        "extractable": extractable,
        "reason": reason,
        "user_lines": user_block,
        "context_lines": context_block if context_text else "",
    })
