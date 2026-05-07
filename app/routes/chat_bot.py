from __future__ import annotations

import os
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from pathlib import Path
from app.assistant.utils.path_utils import get_resources_dir, setup_complete
from app.assistant.utils.assistant_name import get_assistant_name
import json
from datetime import datetime, time, timedelta, timezone

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_time, to_rfc3339_z, utc_now, utc_to_local

logger = get_logger(__name__)

chat_bot_bp = Blueprint('chat_bot', __name__)

CONTROL_RESOURCE_FILENAME = "resource_screen_capture_control.json"


def is_first_time_user():
    """Check if this is a first-time user (no setup completed)."""
    return not setup_complete()



def _dev_tools_enabled() -> bool:
    try:
        # Primary: explicit env override (works even if Flask debug is off)
        if str(os.environ.get("EMI_DEV_TOOLS", "")).strip().lower() in {"1", "true", "yes", "y", "on"}:
            return True

        # If running locally, we still want DEV menu even when `debug=False`
        # (your `run_flask.py` uses debug=False for stability).
        try:
            host = (request.host or "").split(":", 1)[0].strip().lower()
            if host in {"127.0.0.1", "localhost"}:
                return True
        except Exception:
            pass

        return bool(current_app.debug or current_app.config.get("DEBUG", False) or current_app.config.get("TESTING", False))
    except Exception:
        return False


def _wants_mobile_ui() -> bool:
    """
    Decide whether to serve the mobile-first chat UI.

    Priority:
    - Explicit override: ?ui=mobile or ?ui=desktop
    - Otherwise, basic UA heuristic for phones.

    Desktop layout (horizontal vs vertical) is handled client-side
    via JS CSS switching based on viewport aspect ratio.
    """
    try:
        ui = str(request.args.get("ui", "")).strip().lower()
        if ui in {"mobile", "m"}:
            return True
        if ui in {"desktop", "d"}:
            return False

        ua = (request.headers.get("User-Agent") or "").lower()
        return ("mobi" in ua) or ("iphone" in ua) or ("android" in ua)
    except Exception:
        return False


def _control_path():
    # Keep consistent with other status resources.
    return get_resources_dir() / "status" / CONTROL_RESOURCE_FILENAME


def _default_control():
    return {
        "schema_version": 1,
        "enabled": False,
        "enabled_at_utc": None,
        "enabled_by": None,
        "disabled_at_utc": None,
        "disabled_by": None,
        "disabled_reason": None,
        "auto_disabled_date_local": None,
    }


def _load_control() -> dict:
    path = _control_path()
    if not path.exists():
        return _default_control()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_control()
        merged = _default_control()
        merged.update(data)
        return merged
    except Exception:
        return _default_control()


def _save_control(data: dict) -> None:
    path = _control_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _auto_disable_at_0830_local_if_needed(control: dict) -> tuple[dict, bool]:
    """
    Auto-disable once per local day at 08:30.
    Returns (control, changed).
    """
    try:
        if not bool(control.get("enabled", False)):
            return control, False
        now_local = get_local_time()
        today = now_local.date().isoformat()
        if control.get("auto_disabled_date_local") == today:
            return control, False
        if now_local.time() >= time(8, 30):
            c = dict(control)
            c["enabled"] = False
            c["disabled_at_utc"] = to_rfc3339_z(utc_now())
            c["disabled_by"] = "system"
            c["disabled_reason"] = "auto_off_08_30_local"
            c["auto_disabled_date_local"] = today
            return c, True
    except Exception:
        return control, False
    return control, False


@chat_bot_bp.route('/chat_bot', methods=['POST', 'GET'])
def chat_bot():
    # Redirect first-time users to setup wizard
    if is_first_time_user():
        return redirect(url_for('setup.setup_wizard'))
    
    # Get assistant name from config
    assistant_name = get_assistant_name()
    
    # Phone → mobile template (different HTML + JS entirely)
    # Desktop → horizontal template (vertical monitors redirect client-side to ?ui=vertical)
    from app.assistant.user_settings_manager.user_settings import is_feature_enabled
    dev_tools = _dev_tools_enabled()
    music_enabled = is_feature_enabled("music")

    if _wants_mobile_ui():
        return render_template(
            'mobile/chat_bot.html',
            assistant_name=assistant_name,
            dev_tools_enabled=dev_tools,
        )

    ui = str(request.args.get("ui", "")).strip().lower()
    if ui in {"vertical", "v"}:
        return render_template(
            'chat_bot.html',
            assistant_name=assistant_name,
            dev_tools_enabled=dev_tools,
            music_enabled=music_enabled,
        )

    # Default: horizontal desktop layout
    # (vertical monitors auto-redirect via JS in the template <head>)
    return render_template(
        'chat_bot_desktop.html',
        assistant_name=assistant_name,
        dev_tools_enabled=dev_tools,
        music_enabled=music_enabled,
    )


@chat_bot_bp.route('/task/create', methods=['GET'])
def task_create_page():
    return render_template('task_create.html')


@chat_bot_bp.route('/doc/editor', methods=['GET'])
def doc_editor_page():
    return render_template('doc_editor.html')


@chat_bot_bp.route("/api/dev/screen-capture-control", methods=["GET"])
def api_get_screen_capture_control():
    if not _dev_tools_enabled():
        return jsonify({"success": False, "message": "Not found"}), 404

    control = _load_control()
    control, changed = _auto_disable_at_0830_local_if_needed(control)
    if changed:
        try:
            _save_control(control)
        except Exception:
            pass

    return jsonify(
        {
            "success": True,
            "enabled": bool(control.get("enabled", False)),
            "control": control,
            "auto_off_time_local": "08:30",
        }
    )


@chat_bot_bp.route("/api/dev/screen-capture-control", methods=["POST"])
def api_set_screen_capture_control():
    if not _dev_tools_enabled():
        return jsonify({"success": False, "message": "Not found"}), 404

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or "enabled" not in payload:
        return jsonify({"success": False, "message": "Missing 'enabled' boolean"}), 400

    desired = bool(payload.get("enabled", False))
    reason = str(payload.get("reason") or "").strip() or None

    control = _load_control()
    now_utc_iso = to_rfc3339_z(utc_now())

    if desired:
        control["enabled"] = True
        control["enabled_at_utc"] = now_utc_iso
        control["enabled_by"] = "ui"
        control["disabled_at_utc"] = None
        control["disabled_by"] = None
        control["disabled_reason"] = None
    else:
        control["enabled"] = False
        control["disabled_at_utc"] = now_utc_iso
        control["disabled_by"] = "ui"
        control["disabled_reason"] = reason or "manual_off_ui"

    # Enforce auto-off on write too (never allow a POST to keep it enabled past 08:30)
    control, _ = _auto_disable_at_0830_local_if_needed(control)

    try:
        _save_control(control)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to save control: {e}"}), 500

    return jsonify(
        {
            "success": True,
            "enabled": bool(control.get("enabled", False)),
            "control": control,
        }
    )


@chat_bot_bp.route("/api/chat/history", methods=["GET"])
def api_chat_history():
    """Return recent chat messages so the UI can restore conversation on page load."""
    try:
        DI = current_app.DI
        blackboard = DI.global_blackboard

        cutoff_utc = utc_now() - timedelta(hours=24)
        messages = blackboard.get_recent_chat_since_utc(
            cutoff_utc=cutoff_utc,
            room_id="master_room",
        )

        # Only keep user/assistant chat messages
        history = []
        for msg in messages:
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", None)
            if role not in ("user", "assistant") or not content or not content.strip():
                continue
            ts = getattr(msg, "timestamp", None)
            history.append({
                "role": role,
                "content": content.strip(),
                # Local-time ISO for the UI; backend convention is convert
                # at the boundary (frontend at app/static/js/Emi.js doesn't
                # currently render this field, but the contract is local).
                "timestamp": utc_to_local(ts).isoformat() if ts else None,
            })

        # Return only the last 30 messages to keep it lightweight
        history = history[-30:]
        return jsonify({"success": True, "messages": history})
    except Exception as e:
        logger.error("Failed to load chat history: %s", e)
        logger.debug("chat history exception details", exc_info=True)
        return jsonify({"success": True, "messages": []})
