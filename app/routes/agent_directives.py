"""
Agent Directives Routes

API endpoints for reading and editing per-agent standing directives.
Directives are stored as markdown files in resources/instructions/.
"""

from flask import Blueprint, jsonify, render_template, request
from pathlib import Path

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

agent_directives_bp = Blueprint("agent_directives", __name__)

# Registry of agents that support user-editable directives.
# Maps a URL-safe key to (display_name, resource_filename).
DIRECTIVE_AGENTS = {
    "strategic_planner": {
        "display_name": "Strategic Planner",
        "description": "Dayflow orchestrator — decides what plans/tasks to create and what to ignore.",
        "resource_file": "resource_orchestrator_user_prefs.md",
    },
    "email_parser": {
        "display_name": "Email Parser",
        "description": "Scores incoming emails by importance. Controls what gets surfaced vs filtered.",
        "resource_file": "resource_email_user_prefs.md",
    },
    "personal_admin_planner": {
        "display_name": "Personal Admin Planner",
        "description": "Handles email writing, scheduling, and personal admin tasks.",
        "resource_file": "resource_personal_admin_user_prefs.md",
    },
}


@agent_directives_bp.route("/agent-directives")
def agent_directives_page():
    """Serve the agent directives editor page."""
    return render_template("agent_directives.html")


def _directives_dir() -> Path:
    return get_repo_root() / "resources" / "instructions"


@agent_directives_bp.route("/api/agent-directives", methods=["GET"])
def list_agents():
    """List agents that support directives."""
    directives_dir = _directives_dir()
    agents = []
    for key, info in DIRECTIVE_AGENTS.items():
        file_path = directives_dir / info["resource_file"]
        agents.append({
            "key": key,
            "display_name": info["display_name"],
            "description": info["description"],
            "has_content": file_path.exists() and file_path.stat().st_size > 0,
        })
    return jsonify({"agents": agents})


@agent_directives_bp.route("/api/agent-directives/<agent_key>", methods=["GET"])
def get_directive(agent_key):
    """Get the markdown directive for an agent."""
    info = DIRECTIVE_AGENTS.get(agent_key)
    if not info:
        return jsonify({"error": f"Unknown agent key: {agent_key}"}), 404

    file_path = _directives_dir() / info["resource_file"]
    content = ""
    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")

    return jsonify({
        "key": agent_key,
        "display_name": info["display_name"],
        "description": info["description"],
        "content": content,
    })


@agent_directives_bp.route("/api/agent-directives/<agent_key>", methods=["PUT"])
def save_directive(agent_key):
    """Save the markdown directive for an agent."""
    info = DIRECTIVE_AGENTS.get(agent_key)
    if not info:
        return jsonify({"error": f"Unknown agent key: {agent_key}"}), 404

    data = request.get_json(silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str):
        return jsonify({"error": "content must be a string"}), 400

    file_path = _directives_dir() / info["resource_file"]
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    # Refresh the resource in the resource manager so agents pick up changes immediately.
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        resource_id = info["resource_file"].replace(".md", "").replace(".json", "")
        if hasattr(DI, "resource_manager") and DI.resource_manager:
            DI.resource_manager.refresh_resource(resource_id)
            logger.info("Refreshed resource '%s' after directive save.", resource_id)
    except Exception as e:
        logger.warning("Could not refresh resource after directive save: %s", e)

    return jsonify({"status": "saved", "key": agent_key})
