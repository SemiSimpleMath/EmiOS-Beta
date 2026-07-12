from __future__ import annotations

import json
import yaml
from pathlib import Path

from flask import Blueprint, jsonify, abort, send_from_directory, make_response, request

from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

task_graph_bp = Blueprint(
    "task_graph",
    __name__,
    static_folder="../task_graph_viewer/dist",
    static_url_path="/task-graph/static",
)

_SEARCH_DIRS = [
    "tasks/task_created",
    "tasks",
    "tasks/compile_ladder",
]


def _find_compiled_json(task_id: str) -> Path | None:
    repo_root = Path(get_repo_root())
    for rel in _SEARCH_DIRS:
        candidate = repo_root / rel / f"{task_id}.json"
        if candidate.is_file():
            return candidate
    # Recursive fallback — one level deep under tasks/
    tasks_root = repo_root / "tasks"
    if tasks_root.is_dir():
        for child in tasks_root.iterdir():
            if child.is_dir():
                candidate = child / f"{task_id}.json"
                if candidate.is_file():
                    return candidate
    return None


def _find_task_spec(name: str) -> tuple[Path | None, str]:
    """
    Search for a task spec markdown file by name or ID.

    Searches in this order:
    1. tasks/registry.yaml — matched by id or alias (case-insensitive)
    2. tasks/<name>/task_spec.md — direct folder match by id
    3. Fuzzy: any tasks/<subdir>/task_spec.md where subdir contains the query

    Returns (spec_path, task_id) or (None, "").
    """
    repo_root = Path(get_repo_root())
    query = name.strip().lower()
    if not query:
        return None, ""

    # 1. Registry lookup
    registry_path = repo_root / "tasks" / "registry.yaml"
    if registry_path.is_file():
        try:
            registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
            for entry in registry.get("tasks") or []:
                if not isinstance(entry, dict):
                    continue
                task_id = str(entry.get("id") or "").strip()
                task_file = str(entry.get("task_file") or "").strip()
                entry_name = str(entry.get("name") or "").strip().lower()
                aliases = [str(a).strip().lower() for a in (entry.get("aliases") or []) if a]

                if query in {task_id.lower(), entry_name} or query in aliases:
                    if task_file:
                        spec_path = repo_root / task_file
                        if spec_path.is_file():
                            return spec_path, task_id
                    # Fallback: try tasks/<task_id>/task_spec.md
                    fallback = repo_root / "tasks" / task_id / "task_spec.md"
                    if fallback.is_file():
                        return fallback, task_id
        except Exception as e:
            logger.error("Failed reading task registry for spec load: %s", e)
            logger.debug("task registry load exception", exc_info=True)

    # 2. Direct folder match
    direct = repo_root / "tasks" / query / "task_spec.md"
    if direct.is_file():
        return direct, query

    # Also try sanitised (spaces → underscores)
    sanitised = query.replace(" ", "_")
    direct2 = repo_root / "tasks" / sanitised / "task_spec.md"
    if direct2.is_file():
        return direct2, sanitised

    # 3. Fuzzy folder match (substring)
    tasks_root = repo_root / "tasks"
    if tasks_root.is_dir():
        for child in sorted(tasks_root.iterdir()):
            if not child.is_dir():
                continue
            if query in child.name.lower() or child.name.lower() in query:
                candidate = child / "task_spec.md"
                if candidate.is_file():
                    return candidate, child.name

    return None, ""


@task_graph_bp.route("/task-graph/ir/<task_id>")
def get_task_ir(task_id: str):
    """Return compiled task IR JSON for a given task_id."""
    safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")
    if not safe_id:
        abort(400, "Invalid task_id")

    path = _find_compiled_json(safe_id)
    if path is None:
        logger.warning("Task IR not found for task_id=%s", safe_id)
        abort(404, f"Compiled task '{safe_id}' not found")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed reading task IR for task_id=%s: %s", safe_id, e)
        logger.debug("task IR read exception", exc_info=True)
        abort(500, "Failed to read task IR")

    response = jsonify(data)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@task_graph_bp.route("/task-graph/spec/save", methods=["POST"])
def save_spec_draft():
    """
    Save a task spec to its own directory (tasks/<task_id>/).

    Saves both task_spec.md (markdown) and task_spec.json (structured
    TaskSpec) if the session has a TaskSpec object.

    Accepts: { "markdown": "...", "session_id": "<optional>" }
    Returns: { "draft_id": "<task_id>", "path": "<rel path>" }
    """
    import json as _json
    import re as _re
    from datetime import datetime, timezone

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Request body must be a JSON object")

    markdown = str(payload.get("markdown") or "").strip()
    if not markdown:
        abort(400, "markdown field is required and must be non-empty")

    # Get task_id from the session (set by the spec editor when title changes).
    task_id = ""
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.room_session_manager.services.task_creation_session_service import TaskCreationSessionService
            svc = TaskCreationSessionService(blackboard=DI.global_blackboard)
            task_id = svc.get_task_id(session_id=session_id)
        except Exception as e:
            logger.debug("Could not get task_id from session %s: %s", session_id, e)

    # Fallback: derive from markdown title if session didn't have it.
    if not task_id:
        lines = markdown.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                title = stripped[2:].strip()
                if title and title not in ("[Untitled Task]", "[New Task]"):
                    task_id = _re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
                    break
            if stripped.lower() == "## title" and i + 1 < len(lines):
                title = lines[i + 1].strip()
                if title and title not in ("[New Task]", "[TBD]"):
                    task_id = _re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
                    break
    if not task_id:
        task_id = "untitled_task"

    repo_root = Path(get_repo_root())
    task_dir = repo_root / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    spec_md_path = task_dir / "task_spec.md"
    spec_json_path = task_dir / "task_spec.json"

    # Save markdown.
    try:
        spec_md_path.write_text(markdown, encoding="utf-8")
        logger.info("Saved task_spec.md: %s", spec_md_path)
    except Exception as e:
        logger.error("Failed saving task_spec.md for %s: %s", task_id, e)
        logger.debug("spec md save exception", exc_info=True)
        abort(500, "Failed to save spec markdown")

    # Try to save the TaskSpec JSON from the session.
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.room_session_manager.services.task_creation_session_service import TaskCreationSessionService
            svc = TaskCreationSessionService(blackboard=DI.global_blackboard)
            spec_dict = svc.get_task_spec_object(session_id=session_id)
            if isinstance(spec_dict, dict) and spec_dict:
                spec_json_path.write_text(
                    _json.dumps(spec_dict, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                logger.info("Saved task_spec.json: %s", spec_json_path)
        except Exception as e:
            logger.warning("Could not save task_spec.json for %s: %s", task_id, e)

    rel_path = str(spec_md_path.relative_to(repo_root))
    return jsonify({"draft_id": task_id, "path": rel_path})


@task_graph_bp.route("/task-graph/spec/list", methods=["GET"])
def list_specs():
    """
    List available task specs discovered from tasks/*/ on disk,
    enriched with names/aliases from registry.yaml overrides.

    Returns:
        { "tasks": [ { "task_id": "...", "name": "...", "description": "..." }, ... ] }
    """
    from app.assistant.utils.task_registry import load_task_registry
    entries = load_task_registry()
    return jsonify({
        "tasks": [
            {
                "task_id": e.task_id,
                "name": e.name,
                "description": e.description or "",
            }
            for e in sorted(entries, key=lambda e: e.name.lower())
        ]
    })


@task_graph_bp.route("/task-graph/spec/init", methods=["POST"])
def init_spec():
    """
    Initialize a new task spec session or load an existing one.

    Accepts: { "task_id": "<optional — omit to create new>" }
    Returns: { "task_id": "...", "room_id": "...", "spec_markdown": "..." }
    """
    from app.assistant.lib.task_utils.task_spec_store import (
        create_task_spec, load_task_spec_draft, make_room_id,
    )

    payload = request.get_json(silent=True) or {}
    task_id = str(payload.get("task_id") or "").strip()

    if task_id:
        # Try loading existing draft from unified_log.
        draft = load_task_spec_draft(task_id)
        if draft:
            return jsonify(draft)

    # Create new.
    result = create_task_spec(task_id=task_id or None, caller="init_spec")
    return jsonify(result)


@task_graph_bp.route("/task-graph/spec/load", methods=["GET"])
def load_spec():
    """
    Load an existing task spec by name or ID.

    Query params:
        name — task name, ID, or alias to search for

    Returns:
        { "task_id": "...", "spec_markdown": "...", "compiled_task_id": "..." | null }
    """
    name = str(request.args.get("name") or "").strip()
    if not name:
        abort(400, "name query parameter is required")

    spec_path, task_id = _find_task_spec(name)
    if spec_path is None:
        return jsonify({"error": f"No task spec found for '{name}'"}), 404

    try:
        spec_markdown = spec_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed reading task spec for name=%s path=%s: %s", name, spec_path, e)
        logger.debug("task spec read exception", exc_info=True)
        abort(500, "Failed to read task spec")

    compiled_task_id: str | None = None
    if task_id:
        compiled_path = _find_compiled_json(task_id)
        if compiled_path is not None:
            compiled_task_id = task_id

    # Persist to unified_log so the room can find it across turns.
    from app.assistant.lib.task_utils.task_spec_store import save_task_spec_draft, make_room_id
    result = save_task_spec_draft(task_id, spec_markdown=spec_markdown, caller="load_spec")
    room_id = make_room_id(task_id)

    logger.info("load_spec: task_id=%s room_id=%s compiled=%s", task_id, room_id, compiled_task_id)
    return jsonify({
        "task_id": task_id,
        "room_id": room_id,
        "spec_markdown": spec_markdown,
        "compiled_task_id": compiled_task_id,
    })


@task_graph_bp.route("/task-graph/spec/compile", methods=["POST"])
def compile_spec():
    """
    Compile a task spec markdown immediately, bypassing the chat flow.

    Accepts: { "markdown": "...", "session_id": "<optional>" }
    Returns: { "session_id": "...", "status": "compiling" }
    Fires the compile in a background thread and emits task_compiling / task_compiled
    events over the caller's socket.
    """
    import uuid as _uuid

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Request body must be a JSON object")

    markdown = str(payload.get("markdown") or "").strip()
    if not markdown:
        abort(400, "markdown field is required and must be non-empty")

    socket_id = str(payload.get("socket_id") or "").strip()
    raw_session = str(payload.get("session_id") or "").strip()
    session_id = raw_session if raw_session else "task_" + _uuid.uuid4().hex[:12]

    import threading
    from app.assistant.lib.task_utils.task_create_compile_runner import TaskCreateCompileRunner

    # TaskCreateCompileRunner.run takes room_id, not socket_id — the old kwargs raised a
    # TypeError in the background thread on every call (verification finding, pre-existing).
    t = threading.Thread(
        target=TaskCreateCompileRunner.run,
        kwargs=dict(session_id=session_id, spec_markdown=markdown, room_id=""),
        daemon=True,
        name=f"task-compile-{session_id}",
    )
    t.start()
    logger.info("compile_spec: kicked off compile thread session_id=%s socket_id=%s", session_id, socket_id or "(none)")

    return jsonify({"session_id": session_id, "status": "compiling"})


@task_graph_bp.route("/task-graph/ir/<task_id>", methods=["PUT"])
def put_task_ir(task_id: str):
    """
    Save an edited task IR back to disk.

    Accepts: JSON body — the full IR object (legacy steps shape or a work object).
    Returns: the saved IR with updated `updated_at_utc`.
    For the legacy steps shape it also enqueues a background post-processing pass
    that re-materializes data_bindings (work objects carry their edges inline).
    """
    safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")
    if not safe_id:
        abort(400, "Invalid task_id")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Request body must be a JSON object")

    # Basic sanity — either the legacy steps shape or a work object (driver + nodes)
    is_work_object = payload.get("driver") == "task_runner" and isinstance(payload.get("nodes"), list)
    if not is_work_object and not isinstance(payload.get("steps"), list):
        abort(400, "IR must contain a 'steps' array or be a work object (driver + nodes)")

    # Stamp updated time
    from datetime import datetime, timezone
    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()

    # Determine write path — prefer existing location, else default
    existing_path = _find_compiled_json(safe_id)
    if existing_path is not None:
        write_path = existing_path
    else:
        repo_root = Path(get_repo_root())
        out_dir = repo_root / "tasks" / "task_created"
        out_dir.mkdir(parents=True, exist_ok=True)
        write_path = out_dir / f"{safe_id}.json"

    try:
        write_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Saved task IR task_id=%s to %s", safe_id, write_path)
    except Exception as e:
        logger.error("Failed writing task IR for task_id=%s: %s", safe_id, e)
        logger.debug("task IR write exception", exc_info=True)
        abort(500, "Failed to write task IR")

    # Fire post-processing asynchronously (steps-shape only: it re-materializes data bindings
    # on the legacy IR; a work object carries its edges/watches inline and needs none)
    if not is_work_object:
        _fire_post_process(ir=payload, ir_path=write_path)

    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def _fire_post_process(ir: dict, ir_path: Path) -> None:
    """
    Run TaskCompilePostNode in a background thread on the edited IR.
    This re-materializes watch_registrations and data_bindings without
    re-running the LLM compile phases.
    """
    import threading

    def _run() -> None:
        try:
            from app.assistant.lib.task_utils.task_create_compile_runner import TaskCreateCompileRunner
            # Post-process accepts the already-compiled IR dict directly
            TaskCreateCompileRunner.post_process_ir(ir=ir, ir_path=ir_path)
        except Exception as e:
            logger.error("Post-process after IR save failed for %s: %s", ir_path, e)
            logger.debug("Post-process exception", exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name=f"ir-post-process-{ir.get('task_id','?')}")
    t.start()


@task_graph_bp.route("/task-graph/", defaults={"path": ""})
@task_graph_bp.route("/task-graph/<path:path>")
def serve_task_graph_app(path: str):
    """Serve the task graph React SPA and its static assets."""
    dist_dir = Path(__file__).parent.parent / "task_graph_viewer" / "dist"
    if not dist_dir.is_dir():
        abort(503, "Task graph viewer not built. Run: cd app/task_graph_viewer && npm run build")

    # Serve a real file if it exists (assets/index-xxx.js, assets/index-xxx.css, etc.)
    if path:
        candidate = dist_dir / path
        if candidate.is_file():
            return send_from_directory(str(dist_dir), path)

    # Everything else falls through to index.html (SPA routing)
    index = dist_dir / "index.html"
    if not index.is_file():
        abort(503, "Task graph viewer index.html not found")
    response = make_response(index.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/html"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response
