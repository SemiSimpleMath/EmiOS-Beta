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


# ---------------------------------------------------------------------------
# /kg-interests/test/recent-windows — list recent chat windows for picking.
# /kg-interests/test/window/<id> — load one window's user/context lines plus
# the historical critic verdict (so you can compare live vs. stored).
# ---------------------------------------------------------------------------

@kg_interests_bp.route("/kg-interests/test/recent-windows", methods=["GET"])
def kg_interests_test_recent_windows():
    from sqlalchemy import text as sql_text
    from app.models.db_manager import get_db_manager

    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    with get_db_manager().read_session() as s:
        rows = s.execute(sql_text(
            """
            SELECT w.id, w.start_timestamp, w.end_timestamp, w.message_count,
                   w.summary, e.verdict, e.verdict_reason
            FROM kg_window w
            LEFT JOIN kg_window_extraction e ON e.window_id = w.id
            ORDER BY w.start_timestamp DESC
            LIMIT :lim
            """
        ), {"lim": limit}).fetchall()

    out = []
    for r in rows:
        out.append({
            "window_id": r.id,
            "start_timestamp": r.start_timestamp.isoformat() if r.start_timestamp else None,
            "end_timestamp": r.end_timestamp.isoformat() if r.end_timestamp else None,
            "message_count": r.message_count,
            "summary": (r.summary or "")[:140],
            "historical_verdict": r.verdict,
            "historical_reason": (r.verdict_reason or "")[:240] if r.verdict_reason else "",
        })
    return jsonify({"ok": True, "windows": out})


@kg_interests_bp.route("/kg-interests/test/window/<window_id>", methods=["GET"])
def kg_interests_test_window_load(window_id: str):
    """Build user_lines + context_lines for a specific window, exactly as
    the kg_pipeline does (see critique_and_extract._load_window_lines).
    Also returns the historical critic verdict if one is stored."""
    from sqlalchemy import text as sql_text
    from app.models.db_manager import get_db_manager

    if not window_id or not window_id.strip():
        return jsonify({"ok": False, "error": "window_id required"}), 400

    with get_db_manager().read_session() as s:
        # Verify window exists.
        win = s.execute(sql_text(
            "SELECT id, summary, start_timestamp, end_timestamp, message_count "
            "FROM kg_window WHERE id = :w"
        ), {"w": window_id}).fetchone()
        if win is None:
            return jsonify({"ok": False, "error": f"window not found: {window_id}"}), 404

        # Pull messages in order.
        messages = s.execute(sql_text(
            """
            SELECT wm.item_order, ul.role, ul.message, ul.speaker_name
            FROM kg_window_message wm
            JOIN unified_log_2026 ul ON ul.id = wm.unified_log_id
            WHERE wm.window_id = :w
            ORDER BY wm.item_order ASC
            """
        ), {"w": window_id}).fetchall()

        verdict_row = s.execute(sql_text(
            "SELECT verdict, verdict_reason FROM kg_window_extraction WHERE window_id = :w"
        ), {"w": window_id}).fetchone()

    user_lines: list[str] = []
    context_lines: list[str] = []
    for m in messages:
        role = (m.role or "").strip().lower()
        text_val = (m.message or "").strip()
        speaker = (m.speaker_name or "").strip()
        if not text_val:
            continue
        if role == "user":
            if speaker and speaker.lower() != "you":
                user_lines.append(f"{speaker}: {text_val}")
            else:
                user_lines.append(text_val)
        elif role == "assistant":
            context_lines.append(text_val)

    return jsonify({
        "ok": True,
        "window_id": window_id,
        "summary": win.summary or "",
        "start_timestamp": win.start_timestamp.isoformat() if win.start_timestamp else None,
        "user_lines": "\n".join(user_lines),
        "context_lines": "\n".join(context_lines),
        "historical_verdict": verdict_row.verdict if verdict_row else None,
        "historical_reason": (verdict_row.verdict_reason or "")[:1000] if verdict_row else "",
    })
