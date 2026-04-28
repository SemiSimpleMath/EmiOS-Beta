from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.user_settings_manager.user_settings import get_settings_manager

agent_prompt_debug_bp = Blueprint("agent_prompt_debug", __name__)


def _get_agents_snapshot() -> list[dict]:
    agents = []
    try:
        registry = DI.agent_registry
        configs = registry.get_all_agents() if registry else {}
    except Exception:
        configs = {}
    if isinstance(configs, dict):
        for name, cfg in configs.items():
            if not isinstance(name, str):
                continue
            prompt_debug = {}
            if isinstance(cfg, dict):
                pd = cfg.get("prompt_debug")
                if isinstance(pd, dict):
                    prompt_debug = pd
            agents.append({"name": name, "prompt_debug": prompt_debug})
    agents.sort(key=lambda x: x.get("name", ""))
    return agents


@agent_prompt_debug_bp.route("/debug/agent-prompts", methods=["GET"])
def agent_prompt_debug_page():
    return render_template("agent_prompt_debug.html")


@agent_prompt_debug_bp.route("/debug/agent-prompts/data", methods=["GET"])
def agent_prompt_debug_data():
    settings_mgr = get_settings_manager()
    prompt_debug = settings_mgr.get("prompt_debug", {}) if settings_mgr else {}
    return jsonify(
        {
            "agents": _get_agents_snapshot(),
            "prompt_debug": prompt_debug if isinstance(prompt_debug, dict) else {},
        }
    )


@agent_prompt_debug_bp.route("/debug/agent-prompts/default", methods=["PUT"])
def agent_prompt_debug_set_default():
    data = request.get_json(silent=True) or {}
    system = data.get("system")
    user = data.get("user")
    results = data.get("results")
    if not isinstance(system, bool) or not isinstance(user, bool) or not isinstance(results, bool):
        return jsonify({"error": "system, user, and results must be boolean"}), 400
    settings_mgr = get_settings_manager()
    prompt_debug = settings_mgr.get("prompt_debug", {}) if settings_mgr else {}
    if not isinstance(prompt_debug, dict):
        prompt_debug = {}
    prompt_debug["default"] = {"system": bool(system), "user": bool(user), "results": bool(results)}
    if settings_mgr:
        settings_mgr.set("prompt_debug", prompt_debug)
    return jsonify({"ok": True, "prompt_debug": prompt_debug})


@agent_prompt_debug_bp.route("/debug/agent-prompts/agent", methods=["PUT"])
def agent_prompt_debug_set_agent():
    data = request.get_json(silent=True) or {}
    agent_name = data.get("agent_name")
    if not isinstance(agent_name, str) or not agent_name.strip():
        return jsonify({"error": "agent_name is required"}), 400
    remove = bool(data.get("remove", False))
    system = data.get("system")
    user = data.get("user")
    results = data.get("results")

    settings_mgr = get_settings_manager()
    prompt_debug = settings_mgr.get("prompt_debug", {}) if settings_mgr else {}
    if not isinstance(prompt_debug, dict):
        prompt_debug = {}
    agents_cfg = prompt_debug.get("agents", {})
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}

    if remove:
        agents_cfg.pop(agent_name, None)
    else:
        if not isinstance(system, bool) or not isinstance(user, bool) or not isinstance(results, bool):
            return jsonify({"error": "system, user, and results must be boolean"}), 400
        agents_cfg[agent_name] = {"system": bool(system), "user": bool(user), "results": bool(results)}

    prompt_debug["agents"] = agents_cfg
    if settings_mgr:
        settings_mgr.set("prompt_debug", prompt_debug)
    return jsonify({"ok": True, "prompt_debug": prompt_debug})
