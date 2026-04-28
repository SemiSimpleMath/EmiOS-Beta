"""
Debug Logging Route - Runtime logging controls for dev/testing.

Provides:
- /debug/logging (UI page)
- /debug/logging/data (JSON with runtime loggers + current settings)
- /debug/logging/console (PUT set console threshold)
- /debug/logging/override (PUT set/remove per-logger override)
"""

from flask import Blueprint, render_template, jsonify, request

from app.assistant.user_settings_manager.user_settings import get_settings_manager
from app.assistant.utils.logging_config import (
    list_runtime_loggers,
    apply_logging_controls,
    set_console_verbose,
    get_console_verbose,
)


debug_logging_bp = Blueprint("debug_logging", __name__)


@debug_logging_bp.route("/debug/logging")
def logging_page():
    return render_template("debug_logging.html")


@debug_logging_bp.route("/debug/logging/data")
def logging_data():
    settings = get_settings_manager()
    console_level = settings.get("logging.console_level", "WARNING")
    overrides = settings.get("logging.overrides", {}) or {}

    prefix = request.args.get("prefix") or None
    loggers = list_runtime_loggers(prefix=prefix)

    return jsonify({
        "console_level": console_level,
        "overrides": overrides,
        "loggers": loggers,
    })


@debug_logging_bp.route("/debug/logging/console", methods=["PUT"])
def set_console_threshold():
    settings = get_settings_manager()
    data = request.get_json(force=True, silent=True) or {}
    level = data.get("level")
    if not level:
        return jsonify({"success": False, "error": 'Missing "level"'}), 400

    # Persist
    settings.set("logging.console_level", str(level).upper())

    # Apply now
    overrides = settings.get("logging.overrides", {}) or {}
    apply_logging_controls(console_level=str(level).upper(), overrides=overrides)

    return jsonify({"success": True, "console_level": str(level).upper()})


@debug_logging_bp.route("/debug/logging/override", methods=["PUT"])
def set_logger_override():
    """
    Body:
      { "logger": "app.assistant.dj_manager.manager", "level": "DEBUG" }
    To remove override:
      { "logger": "app.assistant.dj_manager.manager", "level": null }
    """
    settings = get_settings_manager()
    data = request.get_json(force=True, silent=True) or {}
    logger_name = data.get("logger")
    if not logger_name or not isinstance(logger_name, str):
        return jsonify({"success": False, "error": 'Missing/invalid "logger"'}), 400

    level = data.get("level", None)

    overrides = settings.get("logging.overrides", {}) or {}
    # Normalize key storage as plain string
    logger_name = logger_name.strip()

    if level is None or (isinstance(level, str) and level.strip() == ""):
        # remove override
        if logger_name in overrides:
            overrides.pop(logger_name, None)
    else:
        overrides[logger_name] = str(level).upper()

    settings.set("logging.overrides", overrides)

    # Apply now (including console threshold)
    console_level = settings.get("logging.console_level", "WARNING")
    apply_logging_controls(console_level=console_level, overrides=overrides)

    return jsonify({"success": True, "logger": logger_name, "overrides": overrides})


@debug_logging_bp.route("/debug/logging/bulk", methods=["PUT"])
def bulk_overrides():
    """
    Bulk set/clear overrides by logger prefix.

    Body:
      { "prefix": "app.assistant.dayflow_manager", "action": "set", "level": "OFF" }
      { "prefix": "httpx", "action": "clear" }

    Notes:
    - Operates on runtime loggers matching the prefix.
    - Skips placeholder-only entries.
    """
    settings = get_settings_manager()
    data = request.get_json(force=True, silent=True) or {}

    prefix = (data.get("prefix") or "").strip()
    action = (data.get("action") or "").strip().lower()
    level = data.get("level", None)

    if not prefix:
        return jsonify({"success": False, "error": 'Missing "prefix"'}), 400
    if action not in ("set", "clear"):
        return jsonify({"success": False, "error": 'Invalid "action" (use "set" or "clear")'}), 400
    if action == "set" and (level is None or (isinstance(level, str) and not level.strip())):
        return jsonify({"success": False, "error": 'Missing "level" for action=set'}), 400

    overrides = settings.get("logging.overrides", {}) or {}

    runtime = list_runtime_loggers(prefix=prefix)
    names = [l["name"] for l in runtime if not l.get("placeholder")]

    changed = 0
    if action == "clear":
        for name in names:
            if name in overrides:
                overrides.pop(name, None)
                changed += 1
    else:
        lvl = str(level).upper()
        for name in names:
            if overrides.get(name) != lvl:
                overrides[name] = lvl
                changed += 1

    settings.set("logging.overrides", overrides)

    console_level = settings.get("logging.console_level", "WARNING")
    apply_logging_controls(console_level=console_level, overrides=overrides)

    return jsonify({
        "success": True,
        "prefix": prefix,
        "action": action,
        "level": str(level).upper() if level is not None else None,
        "matched": len(names),
        "changed": changed,
        "overrides": overrides,
    })


@debug_logging_bp.route("/debug/logging/batch", methods=["PUT"])
def batch_overrides():
    """
    Batch set/clear overrides for an explicit list of logger names.

    Body:
      { "loggers": ["a", "b"], "level": "DEBUG" }   # set level for each logger
      { "loggers": ["a", "b"], "level": null }      # clear overrides for each logger
    """
    settings = get_settings_manager()
    data = request.get_json(force=True, silent=True) or {}

    loggers = data.get("loggers")
    if not isinstance(loggers, list) or not loggers:
        return jsonify({"success": False, "error": 'Missing/invalid "loggers" (must be non-empty list)'}), 400

    # Normalize names
    names = []
    for x in loggers:
        if not isinstance(x, str):
            continue
        n = x.strip()
        if n:
            names.append(n)
    if not names:
        return jsonify({"success": False, "error": "No valid logger names provided"}), 400

    level = data.get("level", None)
    overrides = settings.get("logging.overrides", {}) or {}

    changed = 0
    if level is None or (isinstance(level, str) and not level.strip()):
        for name in names:
            if name in overrides:
                overrides.pop(name, None)
                changed += 1
        action = "clear"
        level_out = None
    else:
        lvl = str(level).upper()
        for name in names:
            if overrides.get(name) != lvl:
                overrides[name] = lvl
                changed += 1
        action = "set"
        level_out = lvl

    settings.set("logging.overrides", overrides)

    console_level = settings.get("logging.console_level", "WARNING")
    apply_logging_controls(console_level=console_level, overrides=overrides)

    return jsonify({
        "success": True,
        "action": action,
        "level": level_out,
        "matched": len(names),
        "changed": changed,
        "overrides": overrides,
    })


@debug_logging_bp.route("/debug/logging/verbose", methods=["GET"])
def get_verbose_state():
    return jsonify({"success": True, "verbose": get_console_verbose()})


@debug_logging_bp.route("/debug/logging/verbose", methods=["POST"])
def toggle_verbose():
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get("enabled")
    if enabled is None:
        enabled = not get_console_verbose()
    result = set_console_verbose(bool(enabled))
    return jsonify({"success": True, "verbose": result})
