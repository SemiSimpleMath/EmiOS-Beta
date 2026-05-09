# app/routes/preferences.py
"""
User and Assistant preferences management
"""
from flask import Blueprint, render_template, request, jsonify
from pathlib import Path
from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir
from app.assistant.utils.path_utils import get_configs_dir
import json
import os
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

preferences_bp = Blueprint('preferences', __name__)
logger = get_logger(__name__)


_IMPORTANT_RESOURCE_SPECS = {
    "resource_user_email": {
        "title": "Email Identity Instructions",
        "description": (
            "Authoritative instructions for personal-admin email operations "
            "(identity, signature, contact specifics)."
        ),
        "path": ("user", "resource_user_email.json"),
        "kind": "json_content",
    },
    "resource_user_data": {
        "title": "User Profile Resource",
        "description": "Core user profile data consumed by planning and personalization.",
        "path": ("user", "resource_user_data.json"),
        "kind": "json_blob",
    },
    "assistant_core": {
        "title": "Assistant Core Resource",
        "description": "Assistant identity, relationship, and chat guideline source of truth.",
        "path": ("assistant", "assistant_core.json"),
        "kind": "json_blob",
    },
    "resource_chat_guidelines_data": {
        "title": "Chat Guidelines Resource",
        "description": "Global chat style directives used across rooms.",
        "path": ("assistant", "resource_chat_guidelines_data.json"),
        "kind": "json_blob",
    },
    "resource_orchestrator_user_prefs": {
        "title": "Orchestrator Preferences (Dayflow)",
        "description": (
            "Personal directives the dayflow orchestrator and chat_gate read as context."
        ),
        "path": ("instructions", "resource_orchestrator_user_prefs.md"),
        "kind": "markdown",
    },
    "resource_assistant_guidelines": {
        "title": "Top-level Assistant Guidelines",
        "description": "Complexity ladder (C1-C5), operational guardrails (B1-B5), execution logic.",
        "path": ("instructions", "resource_assistant_guidelines.md"),
        "kind": "markdown",
    },
    "resource_auto_planner_instructions": {
        "title": "Auto-Planner Instructions",
        "description": "Guidelines for when the planner should ask first vs. take action.",
        "path": ("instructions", "resource_auto_planner_instructions.md"),
        "kind": "markdown",
    },
    "resource_activity_log": {
        "title": "Activity Log Behavior",
        "description": "Guidelines for the desktop activity tracker.",
        "path": ("instructions", "resource_activity_log.md"),
        "kind": "markdown",
    },
    "resource_bbc_guidelines": {
        "title": "BBC Browser Hints",
        "description": "Site-specific hints for the playwright agent on BBC News.",
        "path": ("instructions", "resource_bbc_guidelines.md"),
        "kind": "markdown",
    },
    "resource_cnn_guidelines": {
        "title": "CNN Browser Hints",
        "description": "Site-specific hints for the playwright agent on CNN.",
        "path": ("instructions", "resource_cnn_guidelines.md"),
        "kind": "markdown",
    },
    "resource_doordash_guidelines": {
        "title": "DoorDash Browser Hints",
        "description": "Site-specific hints for the playwright agent ordering food on DoorDash.",
        "path": ("instructions", "resource_doordash_guidelines.md"),
        "kind": "markdown",
    },
}

def get_resources_dir():
    """Get the resources directory path"""
    return _get_resources_dir()


def _read_env_file(env_file: Path) -> dict:
    """Parse .env file into a key→value dict, preserving all existing keys."""
    content = {}
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    content[key.strip()] = value.strip()
    return content


def _write_env_file(env_file: Path, env_content: dict) -> None:
    """Write env_content dict back to the .env file preserving all keys."""
    # Preserve existing lines (comments + order) by rewriting the whole file.
    # We keep a defined section order and dump remaining keys at the end.
    KNOWN_SECTIONS = [
        ("# Core Settings", ["OPENAI_API_KEY", "TIMEZONE", "PRIMARY_USER", "ASSISTANT_NAME"]),
        ("# Optional API Keys", ["GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY"]),
        ("# Email / Gmail Integration", ["EMAIL_ADDR", "EMAIL_PASSWORD", "EMAIL_IMAP_URL"]),
        ("# Telegram", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"]),
        ("# Runtime Flags", ["SCOPE_CONTRACT_STRICT"]),
    ]
    written: set[str] = set()

    with open(env_file, "w", encoding="utf-8") as f:
        f.write("# Emi Configuration\n\n")
        for section_comment, keys in KNOWN_SECTIONS:
            section_lines = []
            for key in keys:
                if key in env_content:
                    section_lines.append(f"{key}={env_content[key]}\n")
                    written.add(key)
            if section_lines:
                f.write(f"{section_comment}\n")
                f.writelines(section_lines)
                f.write("\n")

        remaining = [(k, v) for k, v in env_content.items() if k not in written]
        if remaining:
            f.write("# Other\n")
            for key, value in remaining:
                f.write(f"{key}={value}\n")


def _sync_env_var(key: str, value: str) -> None:
    """
    Write a single key=value into the project .env file and update os.environ
    so the running process picks it up immediately without a restart.
    """
    import os
    project_root = get_project_root()
    env_file = project_root / ".env"
    env_content = _read_env_file(env_file)
    env_content[key] = value
    _write_env_file(env_file, env_content)
    os.environ[key] = value

def update_resource_data(resource_id, json_data):
    """
    Update a resource's JSON data file and global blackboard.

    Uses merge semantics: loads existing file, updates with new data, writes
    back.  This prevents callers that send a partial dict from clobbering
    fields they don't manage.

    After data changes, recompile resource templates so all injectable resources stay concrete.
    """
    import traceback as _tb

    resources_dir = get_resources_dir()

    def resolve_resource_path(rid: str) -> Path:
        # Preferences UI edits are intended for setup/profile-ish resources,
        # which live in organized subfolders.
        if rid == "resource_user_data":
            return resources_dir / "user" / f"{rid}.json"
        if rid in {
            "resource_assistant_data",
            "resource_assistant_personality_data",
            "resource_relationship_config",
            "resource_chat_guidelines_data",
            "assistant_core",
        }:
            return resources_dir / "assistant" / f"{rid}.json"
        # Fallback (legacy/unclassified)
        return resources_dir / f"{rid}.json"

    json_file = resolve_resource_path(resource_id)
    logger.info(
        "update_resource_data('%s') -> %s | caller: %s",
        resource_id,
        json_file,
        "".join(_tb.format_stack(limit=5)).replace("\n", " | "),
    )
    json_file.parent.mkdir(parents=True, exist_ok=True)

    # Merge: load existing file first so callers that send partial dicts
    # don't clobber fields they don't manage.
    existing = {}
    if json_file.exists():
        try:
            existing = json.loads(json_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    if isinstance(json_data, dict):
        merged = dict(existing)
        merged.update(json_data)
    else:
        merged = json_data

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    
    # Update global blackboard immediately.
    if hasattr(DI, 'global_blackboard') and DI.global_blackboard:
        DI.global_blackboard.update_state_value(resource_id, json_data)

    # Recompile template resources now so agents never do runtime composition.
    if hasattr(DI, "resource_manager") and DI.resource_manager:
        try:
            DI.resource_manager.compile_templates("resources")
        except Exception as e:
            logger.error("Template compile failed after updating '%s': %s", resource_id, e)
            raise

def get_project_root():
    """Get the project root directory path"""
    return Path(__file__).resolve().parents[2]


def _smart_home_config_path() -> Path:
    return get_configs_dir() / "smart_home_tools.json"


def _default_smart_home_config() -> dict:
    return {
        "nest": {
            "enabled": False,
            "endpoint_url": "http://127.0.0.1:8000/api/smart-home/nest",
            "token_env_var": "EMI_SMARTHOME_NEST_TOKEN",
            "timeout_seconds": 20,
        },
        "lights": {
            "enabled": False,
            "endpoint_url": "http://127.0.0.1:8000/api/smart-home/lights",
            "token_env_var": "EMI_SMARTHOME_LIGHTS_TOKEN",
            "timeout_seconds": 20,
            # Devices: [{alias, host, notes}]. Written by the lights
            # settings UI; canonical source of truth for the Kasa bridge.
            "devices": [],
            # kasa_device_hosts is the legacy flat host array. New UIs
            # should not write here; the bridge unions it with devices[]
            # for back-compat.
            "kasa_device_hosts": [],
            "kasa_discovery_timeout_seconds": 4,
        },
        "ring": {
            "enabled": False,
            "endpoint_url": "http://127.0.0.1:8000/api/smart-home/ring",
            "token_env_var": "EMI_SMARTHOME_RING_TOKEN",
            "timeout_seconds": 20,
            "devices": [],
        },
        # Local RTSP cameras (Tapo, Wyze, Reolink, etc.). Each device
        # entry: {alias, host, port, path, username, password_env_var,
        # kind}. Distinct from `ring` because Ring cameras go through
        # the Ring cloud API while local cameras stream directly via
        # RTSP. Kept categorically separate so the lights bridge never
        # mistakes a Tapo for a Kasa light, and the Ring routes never
        # try to authenticate against Tapo creds.
        "local_cameras": {
            "enabled": False,
            "endpoint_url": "http://127.0.0.1:8000/api/smart-home/local-cameras",
            "timeout_seconds": 20,
            "devices": [],
        },
    }


def _load_smart_home_config() -> dict:
    path = _smart_home_config_path()
    if not path.exists():
        defaults = _default_smart_home_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.error("Failed reading smart_home_tools config: %s", e)
        logger.debug("smart_home_tools read exception details", exc_info=True)
        raise
    if not isinstance(payload, dict):
        raise ValueError("configs/smart_home_tools.json must be a JSON object.")
    defaults = _default_smart_home_config()
    merged = dict(defaults)
    merged.update(payload)
    return merged


def _save_smart_home_config(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Smart-home config payload must be a dict.")
    path = _smart_home_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Merge with existing config so updating one device doesn't clobber others.
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    merged = dict(existing)
    merged.update(payload)

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed writing smart_home_tools config: %s", e)
        logger.debug("smart_home_tools write exception details", exc_info=True)
        raise

@preferences_bp.route('/preferences/general', methods=['GET'])
def user_settings_page():
    """Render user settings page"""
    return render_template('user_settings.html')

@preferences_bp.route('/settings/assistant', methods=['GET'])
def assistant_settings_page():
    """Render assistant settings page"""
    return render_template('assistant_settings.html')

@preferences_bp.route('/settings/api-keys', methods=['GET'])
def api_keys_page():
    """Render API keys settings page"""
    return render_template('api_keys_settings.html')

@preferences_bp.route('/settings/features', methods=['GET'])
def features_page():
    """Render features settings page"""
    return render_template('features_settings.html')


@preferences_bp.route('/settings/voice', methods=['GET'])
def voice_settings_page():
    """Render voice settings page"""
    return render_template('voice_settings.html')


@preferences_bp.route('/settings/integrations', methods=['GET'])
def integrations_hub_page():
    """Render integrations hub with cards linking to each integration."""
    return render_template('integrations_hub.html')


@preferences_bp.route('/settings/integrations/google', methods=['GET'])
def google_oauth_settings_page():
    """Render Google OAuth primary account + service routing."""
    return render_template('google_oauth_settings.html')


@preferences_bp.route('/settings/integrations/google-accounts', methods=['GET'])
def google_accounts_settings_page():
    """Render additional Google accounts management."""
    return render_template('google_accounts_settings.html')


@preferences_bp.route('/settings/integrations/nest', methods=['GET'])
def nest_settings_page():
    """Render Nest thermostat settings."""
    return render_template('nest_settings.html')


@preferences_bp.route('/settings/integrations/ring', methods=['GET'])
def ring_settings_page():
    """Render Ring cameras settings."""
    return render_template('ring_settings.html')


@preferences_bp.route('/settings/lights', methods=['GET'])
def lights_settings_page():
    """Render dedicated smart lights settings page."""
    return render_template('lights_settings.html')


def _all_configured_camera_aliases() -> list:
    """Read ring + local_cameras device aliases from smart_home_tools.json
    so the snapshots page can render a chip-row of switchable cameras."""
    path = _smart_home_config_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for integration in ("ring", "local_cameras"):
        section = payload.get(integration) if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            continue
        for d in section.get("devices") or []:
            if not isinstance(d, dict):
                continue
            alias = str(d.get("alias") or "").strip()
            if alias:
                out.append({"alias": alias, "integration": integration, "kind": str(d.get("kind") or "")})
    return out


@preferences_bp.route('/settings/integrations/ring/snapshots', methods=['GET'])
def ring_snapshots_page():
    """Browse all camera JPEGs (Ring + local cameras), newest first."""
    return render_template(
        'ring_snapshots.html',
        camera_filter=None,
        configured_cameras=_all_configured_camera_aliases(),
    )


@preferences_bp.route('/cameras/<alias>', methods=['GET'])
def camera_page(alias):
    """Per-camera snapshot view — same template, filtered by camera alias."""
    return render_template(
        'ring_snapshots.html',
        camera_filter=alias,
        configured_cameras=_all_configured_camera_aliases(),
    )


def _camera_snapshot_dirs() -> list:
    """Return the dirs the snapshot UI scans. Ring frames land in
    data/ring_snapshots/; local_camera_snapshot frames land in
    data/local_camera_snapshots/. The UI lists both."""
    repo_root = Path(__file__).resolve().parents[2]
    return [
        repo_root / "data" / "ring_snapshots",
        repo_root / "data" / "local_camera_snapshots",
    ]


def _ring_snapshots_dir():
    """Legacy alias kept for callers that historically only wanted the Ring
    bridge dir. New callers should use _camera_snapshot_dirs() to scan both."""
    return _camera_snapshot_dirs()[0]


def _read_ring_sidecar(jpg_path):
    """Read the .txt sidecar next to a snapshot JPEG (if any).

    Sidecar format written by DoorBellAnalyzer / SleepAnalyzer:
        <caption or notes>
        <blank>
        key: value
        key: value
        ...

    Returns a dict with at least ``caption`` (first non-empty line) plus
    parsed key:value entries. Empty dict if the sidecar doesn't exist.
    """
    sidecar = jpg_path.with_suffix(".txt")
    if not sidecar.exists():
        return {}
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    lines = text.splitlines()
    # First non-empty line is the caption / notes free text.
    caption = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            caption = ln.strip()
            body_start = i + 1
            break
    if caption:
        out["caption"] = caption
    for ln in lines[body_start:]:
        ln = ln.strip()
        if not ln or ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # Coerce a few well-known numerics.
        if k == "importance":
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
        elif k in ("subject_in_bed", "is_significant"):
            out[k] = v.lower() == "true"
        else:
            out[k] = v
    return out


@preferences_bp.route('/api/ring/snapshots', methods=['GET'])
def list_ring_snapshots():
    """List captured snapshots from BOTH Ring and local-camera storage,
    newest first. Optional ``?camera=<alias>`` query param filters by
    camera alias (case-insensitive).

    Filename convention is the same in both folders:
        ``{YYYYMMDDTHHMMSSZ}_{safe_camera_id}.jpg``

    For each JPEG the matching ``.txt`` sidecar (when present) is parsed
    and its fields surfaced — most importantly ``importance`` (0-10) so
    the UI can flag worth-reviewing frames at a glance.
    """
    try:
        camera_filter = (request.args.get("camera") or "").strip().lower()
        entries = []
        for snap_dir in _camera_snapshot_dirs():
            if not snap_dir.exists():
                continue
            for p in snap_dir.iterdir():
                if not p.is_file() or p.suffix.lower() != ".jpg":
                    continue
                stem = p.stem
                if "_" not in stem:
                    continue
                ts_part, _, cam_part = stem.partition("_")
                if len(ts_part) != 16 or not ts_part.endswith("Z"):
                    continue
                if camera_filter and cam_part.lower() != camera_filter:
                    continue
                try:
                    size = p.stat().st_size
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                entry = {
                    "filename": p.name,
                    "camera_id": cam_part,
                    "captured_at_utc": ts_part,
                    "size_bytes": size,
                    "mtime": mtime,
                    "source_dir": snap_dir.name,
                    "sidecar": _read_ring_sidecar(p),
                }
                entries.append(entry)
        entries.sort(key=lambda e: e["mtime"], reverse=True)
        return jsonify({"success": True, "snapshots": entries})
    except Exception as e:
        logger.error("list_ring_snapshots failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@preferences_bp.route('/api/ring/snapshots/image/<path:filename>', methods=['GET'])
def serve_ring_snapshot(filename):
    """Serve one snapshot JPEG. Searches both Ring and local-camera dirs;
    send_from_directory enforces path-traversal safety per dir."""
    from flask import send_from_directory, abort
    for snap_dir in _camera_snapshot_dirs():
        if (snap_dir / filename).exists():
            return send_from_directory(str(snap_dir), filename, mimetype="image/jpeg")
    abort(404)


# --- Smart-home device aliases (read/write the `devices` array per integration) ---

_SH_INTEGRATION_ID_FIELDS = {
    "lights": "host",
    "ring": "camera_id",
    "nest": "device_id",
    # Tapo + other RTSP cameras live under local_cameras. id_field is
    # `host` since they're addressed by IP. Camera-specific fields
    # (port, path, username, password_env_var, kind) flow through the
    # devices CRUD via the `notes` field for now; a dedicated Tapo
    # settings page that exposes them as named columns is the natural
    # next step.
    "local_cameras": "host",
}


def _smart_home_config_path():
    return get_configs_dir() / "smart_home_tools.json"


def _load_smart_home_devices(integration: str) -> list:
    """Read the devices array for one integration. Returns [] if absent."""
    path = _smart_home_config_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("smart_home_tools.json is invalid JSON: %s", e)
        return []
    section = payload.get(integration) if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        return []
    devices = section.get("devices")
    return devices if isinstance(devices, list) else []


def _save_smart_home_devices(integration: str, devices: list) -> None:
    """Atomically replace the devices array for one integration. Other
    integrations' sections are preserved."""
    path = _smart_home_config_path()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("smart_home_tools.json must be a JSON object.")
    section = payload.get(integration)
    if not isinstance(section, dict):
        raise ValueError(f"Integration '{integration}' missing in smart_home_tools.json.")
    section["devices"] = devices
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _validate_devices_payload(integration: str, devices) -> list:
    if not isinstance(devices, list):
        raise ValueError("devices must be a list.")
    id_field = _SH_INTEGRATION_ID_FIELDS.get(integration)
    if id_field is None:
        raise ValueError(f"Unknown integration: {integration}")
    out = []
    seen_aliases = set()
    for d in devices:
        if not isinstance(d, dict):
            raise ValueError("each device must be a JSON object.")
        alias = str(d.get("alias") or "").strip()
        if not alias:
            raise ValueError("each device requires a non-empty 'alias'.")
        if alias.lower() in seen_aliases:
            raise ValueError(f"duplicate alias: {alias}")
        seen_aliases.add(alias.lower())
        raw_id = str(d.get(id_field) or "").strip()
        if not raw_id:
            raise ValueError(f"each device requires a non-empty '{id_field}'.")
        cleaned = {"alias": alias, id_field: raw_id}
        notes = str(d.get("notes") or "").strip()
        if notes:
            cleaned["notes"] = notes
        out.append(cleaned)
    return out


@preferences_bp.route('/api/integrations/<integration>/devices', methods=['GET'])
def get_smart_home_devices(integration):
    if integration not in _SH_INTEGRATION_ID_FIELDS:
        return jsonify({"success": False, "error": f"Unknown integration: {integration}"}), 400
    try:
        devices = _load_smart_home_devices(integration)
        return jsonify({
            "success": True,
            "integration": integration,
            "id_field": _SH_INTEGRATION_ID_FIELDS[integration],
            "devices": devices,
        })
    except Exception as e:
        logger.error("get_smart_home_devices(%s) failed: %s", integration, e)
        return jsonify({"success": False, "error": str(e)}), 500


@preferences_bp.route('/api/integrations/<integration>/devices', methods=['PUT'])
def put_smart_home_devices(integration):
    if integration not in _SH_INTEGRATION_ID_FIELDS:
        return jsonify({"success": False, "error": f"Unknown integration: {integration}"}), 400
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        cleaned = _validate_devices_payload(integration, body.get("devices"))
        _save_smart_home_devices(integration, cleaned)
        return jsonify({"success": True, "integration": integration, "devices": cleaned})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error("put_smart_home_devices(%s) failed: %s", integration, e)
        return jsonify({"success": False, "error": str(e)}), 500


# --- lights_control tool description (editable from /settings/integrations/lights) ---

def _lights_control_description_path():
    """Absolute path to the lights_control tool's description prompt."""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "app" / "assistant" / "lib" / "tools" / "lights_control" / "prompts" / "lights_control_description.j2"


@preferences_bp.route('/api/integrations/lights/tool-description', methods=['GET'])
def get_lights_tool_description():
    try:
        path = _lights_control_description_path()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return jsonify({"success": True, "content": text, "path": str(path)})
    except Exception as e:
        logger.error("get_lights_tool_description failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@preferences_bp.route('/api/integrations/lights/tool-description', methods=['PUT'])
def put_lights_tool_description():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("'content' must be a string.")
        path = _lights_control_description_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return jsonify({"success": True, "path": str(path)})
    except Exception as e:
        logger.error("put_lights_tool_description failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@preferences_bp.route('/api/integrations/nest/config', methods=['GET'])
def get_nest_integration_config():
    return _get_smart_home_integration_config(integration_key="nest", integration_label="Nest")


@preferences_bp.route('/api/integrations/lights/config', methods=['GET'])
def get_lights_integration_config():
    return _get_smart_home_integration_config(integration_key="lights", integration_label="Lights")


@preferences_bp.route('/api/integrations/ring/config', methods=['GET'])
def get_ring_integration_config():
    return _get_smart_home_integration_config(integration_key="ring", integration_label="Ring")


def _get_smart_home_integration_config(*, integration_key: str, integration_label: str):
    try:
        payload = _load_smart_home_config()
        integration_cfg = payload.get(integration_key)
        if not isinstance(integration_cfg, dict):
            raise ValueError(f"Missing '{integration_key}' config in smart_home_tools.json.")
        token_env_var = str(integration_cfg.get("token_env_var") or "").strip()
        # Ring auth is file-based, not env-var-based — the OAuth token is a
        # multi-key dict that ring-doorbell rotates on refresh, so it lives
        # in data/ring_token.json (bootstrapped by scripts/ring_bootstrap.py).
        # Other integrations (nest, lights) still use env vars.
        if integration_key == "ring":
            from pathlib import Path
            repo_root = Path(__file__).resolve().parents[2]
            token_is_set = (repo_root / "data" / "ring_token.json").exists()
        else:
            token_is_set = bool(token_env_var and str(os.environ.get(token_env_var) or "").strip())
        response = {
            "enabled": bool(integration_cfg.get("enabled", False)),
            "endpoint_url": str(integration_cfg.get("endpoint_url") or "").strip(),
            "token_env_var": token_env_var,
            "timeout_seconds": int(integration_cfg.get("timeout_seconds", 20)),
            "token_is_set": token_is_set,
        }
        if integration_key == "lights":
            hosts_raw = integration_cfg.get("kasa_device_hosts", [])
            if hosts_raw is None:
                hosts_raw = []
            if not isinstance(hosts_raw, list):
                raise ValueError("kasa_device_hosts must be a list.")
            response["kasa_device_hosts"] = [
                str(x).strip() for x in hosts_raw if isinstance(x, str) and str(x).strip()
            ]
            response["kasa_discovery_timeout_seconds"] = int(
                integration_cfg.get("kasa_discovery_timeout_seconds", 4)
            )
        return jsonify({"success": True, "config": response})
    except Exception as e:
        logger.error("Failed loading %s integration config: %s", integration_label, e)
        logger.debug("get_%s_integration_config exception details", integration_key, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@preferences_bp.route('/api/integrations/nest/config', methods=['PUT'])
def update_nest_integration_config():
    return _update_smart_home_integration_config(integration_key="nest", integration_label="Nest")


@preferences_bp.route('/api/integrations/lights/config', methods=['PUT'])
def update_lights_integration_config():
    return _update_smart_home_integration_config(integration_key="lights", integration_label="Lights")


@preferences_bp.route('/api/integrations/ring/config', methods=['PUT'])
def update_ring_integration_config():
    return _update_smart_home_integration_config(integration_key="ring", integration_label="Ring")


def _update_smart_home_integration_config(*, integration_key: str, integration_label: str):
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")

        payload = _load_smart_home_config()
        integration_cfg = payload.get(integration_key)
        if not isinstance(integration_cfg, dict):
            integration_cfg = {}

        if "enabled" in data:
            integration_cfg["enabled"] = bool(data.get("enabled"))
        if "endpoint_url" in data:
            endpoint_url = str(data.get("endpoint_url") or "").strip()
            if not endpoint_url:
                raise ValueError("endpoint_url must be non-empty.")
            integration_cfg["endpoint_url"] = endpoint_url
        if "token_env_var" in data:
            token_env_var = str(data.get("token_env_var") or "").strip()
            if not token_env_var:
                raise ValueError("token_env_var must be non-empty.")
            integration_cfg["token_env_var"] = token_env_var
        if "timeout_seconds" in data:
            timeout_seconds_raw = data.get("timeout_seconds")
            try:
                timeout_seconds = int(timeout_seconds_raw)
            except Exception as e:
                logger.error("Invalid timeout_seconds for %s config: %r (%s)", integration_label, timeout_seconds_raw, e)
                logger.debug("update_%s_integration_config timeout parse exception details", integration_key, exc_info=True)
                raise
            if timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be > 0.")
            integration_cfg["timeout_seconds"] = timeout_seconds
        if integration_key == "lights":
            if "kasa_device_hosts" in data:
                hosts_raw = data.get("kasa_device_hosts")
                if hosts_raw is None:
                    hosts_raw = []
                if not isinstance(hosts_raw, list):
                    raise ValueError("kasa_device_hosts must be a list of host strings.")
                hosts = [str(x).strip() for x in hosts_raw if isinstance(x, str) and str(x).strip()]
                integration_cfg["kasa_device_hosts"] = hosts
            if "kasa_discovery_timeout_seconds" in data:
                timeout_raw = data.get("kasa_discovery_timeout_seconds")
                try:
                    kasa_timeout = int(timeout_raw)
                except Exception as e:
                    logger.error(
                        "Invalid kasa_discovery_timeout_seconds for %s config: %r (%s)",
                        integration_label,
                        timeout_raw,
                        e,
                    )
                    logger.debug(
                        "update_%s_integration_config kasa timeout parse exception details",
                        integration_key,
                        exc_info=True,
                    )
                    raise
                if kasa_timeout <= 0:
                    raise ValueError("kasa_discovery_timeout_seconds must be > 0.")
                integration_cfg["kasa_discovery_timeout_seconds"] = kasa_timeout

        payload[integration_key] = integration_cfg
        _save_smart_home_config(payload)
        return jsonify({"success": True, "message": f"{integration_label} integration config updated."})
    except Exception as e:
        logger.error("Failed updating %s integration config: %s", integration_label, e)
        logger.debug("update_%s_integration_config exception details", integration_key, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 400


@preferences_bp.route('/api/integrations/nest/action', methods=['POST'])
def run_nest_integration_action():
    return _run_smart_home_integration_action(integration_key="nest", integration_label="Nest")


@preferences_bp.route('/api/integrations/lights/action', methods=['POST'])
def run_lights_integration_action():
    return _run_smart_home_integration_action(integration_key="lights", integration_label="Lights")


@preferences_bp.route('/api/integrations/ring/action', methods=['POST'])
def run_ring_integration_action():
    return _run_smart_home_integration_action(integration_key="ring", integration_label="Ring")


def _run_smart_home_integration_action(*, integration_key: str, integration_label: str):
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        command = str(data.get("command") or "").strip().lower()
        if not command:
            raise ValueError("command must be provided.")
        arguments = data.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object when provided.")

        from app.assistant.lib.tools.smart_home_gateway import send_smart_home_command

        response_data = send_smart_home_command(
            integration=integration_key,
            command=command,
            arguments=arguments,
            request_id=None,
        )
        return jsonify({"success": True, "data": response_data})
    except Exception as e:
        logger.error("%s integration command failed: %s", integration_label, e)
        logger.debug("run_%s_integration_action exception details", integration_key, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 400


@preferences_bp.route('/settings/quiet-mode', methods=['GET'])
def quiet_mode_page():
    """Render quiet mode settings page"""
    return render_template('quiet_mode_settings.html')


@preferences_bp.route('/settings/wellness', methods=['GET'])
def wellness_settings_page():
    """Render wellness reminders settings page"""
    return render_template('wellness_settings.html')


@preferences_bp.route('/settings/important-resources', methods=['GET'])
def important_resources_page():
    """Render important resources editor page."""
    return render_template('important_resources_settings.html')


# ---------------------------------------------------------------------------
# Skills pages
#   /skills          — overview: three injection patterns + featured editors
#   /skills/email    — dedicated editor for email user prefs (a SKILL: action
#                      instructions for the email agent)
#   /skills/dayflow  — dedicated editor for personal dayflow orchestrator
#                      preferences (a SKILL: action instructions for the
#                      strategic_planner)
# ---------------------------------------------------------------------------

# Map of editor-page → underlying instruction file. Strict allowlist —
# the API only reads/writes these specific paths, never an arbitrary file
# from a request param.
_EDITABLE_SKILL_PATHS = {
    "email":   "resources/instructions/resource_email_user_prefs.md",
    "dayflow": "resources/instructions/resource_orchestrator_user_prefs_personal.md",
}


def _skill_path(skill_id: str) -> Path:
    rel = _EDITABLE_SKILL_PATHS.get(skill_id)
    if not rel:
        raise ValueError(f"unknown editable skill: {skill_id!r}")
    return Path(get_project_root()) / rel


@preferences_bp.route('/skills', methods=['GET'])
def skills_overview_page():
    """Skills overview: lists injection patterns and featured editors.
    The full registry listing is on its own subpage (/skills/all) since
    it's too tall to share screen with the explainer."""
    return render_template('skills_overview.html')


@preferences_bp.route('/skills/all', methods=['GET'])
def skills_all_page():
    """Full registry listing, grouped by injection pattern."""
    return render_template('skills_all.html')


@preferences_bp.route('/skills/email', methods=['GET'])
def skills_email_page():
    """Dedicated editor for the email-agent skill."""
    return render_template('skills_email.html')


@preferences_bp.route('/skills/dayflow', methods=['GET'])
def skills_dayflow_page():
    """Dedicated editor for the dayflow-orchestrator skill."""
    return render_template('skills_dayflow.html')


@preferences_bp.route('/api/skills', methods=['GET'])
def skills_list_api():
    """Catalog of skills for the overview page, grouped by injection pattern.

    Walks SkillRegistry for the loaded markdown skills and walks every
    agent's config.yaml `skills:` field to compute usage. Three buckets:

      - shared        : referenced by 2+ agents
      - agent_specific: referenced by exactly 1 agent (no auto-trigger)
      - keyword       : has metadata.auto_inject_when.task_keywords
    """
    import yaml as _yaml
    from collections import defaultdict

    skill_to_agents: dict[str, list[str]] = defaultdict(list)
    agents_dir = Path(get_project_root()) / "app" / "assistant" / "agents"
    if agents_dir.exists():
        for cfg_path in agents_dir.rglob("config.yaml"):
            try:
                cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            agent_skills = cfg.get("skills") or []
            if not isinstance(agent_skills, list):
                continue
            agent_name = str(cfg.get("name") or "").strip() or cfg_path.parent.name
            for s in agent_skills:
                sname = str(s).strip()
                if sname and agent_name not in skill_to_agents[sname]:
                    skill_to_agents[sname].append(agent_name)

    # Walk SkillRegistry for loaded skills + their auto-trigger metadata.
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        registry = getattr(DI, "skill_registry", None)
        headers = registry.headers() if registry else []
    except Exception:
        headers = []

    shared, agent_specific, keyword = [], [], []
    for h in headers:
        agents = skill_to_agents.get(h.name, [])
        has_keywords = bool(
            h.auto_inject_when and getattr(h.auto_inject_when, "task_keywords", None)
        )
        entry = {
            "name": h.name,
            "description": h.description,
            "agents": agents,
            "task_keywords": (
                list(h.auto_inject_when.task_keywords) if has_keywords else []
            ),
        }
        if has_keywords:
            keyword.append(entry)
        elif len(agents) >= 2:
            shared.append(entry)
        else:
            agent_specific.append(entry)

    # Sort each bucket alphabetically for stable display.
    for bucket in (shared, agent_specific, keyword):
        bucket.sort(key=lambda e: e["name"])

    return jsonify({
        "shared": shared,
        "agent_specific": agent_specific,
        "keyword": keyword,
    })


@preferences_bp.route('/api/skills/<skill_id>', methods=['GET'])
def skill_get_api(skill_id: str):
    """Read the markdown body of an editable skill (email or dayflow)."""
    try:
        path = _skill_path(skill_id)
    except ValueError:
        return jsonify({"error": "unknown skill"}), 404
    if not path.exists():
        return jsonify({"skill_id": skill_id, "content": "", "exists": False})
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed reading skill %s: %s", skill_id, e)
        return jsonify({"error": "read failed"}), 500
    return jsonify({"skill_id": skill_id, "content": content, "exists": True})


@preferences_bp.route('/api/skills/<skill_id>', methods=['PUT'])
def skill_put_api(skill_id: str):
    """Atomic write of an editable skill's markdown body."""
    try:
        path = _skill_path(skill_id)
    except ValueError:
        return jsonify({"error": "unknown skill"}), 404
    payload = request.get_json(silent=True) or {}
    content = str(payload.get("content") or "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
            f.flush()
        tmp.replace(path)
    except Exception as e:
        logger.error("Failed writing skill %s: %s", skill_id, e)
        return jsonify({"error": "write failed"}), 500
    return jsonify({"ok": True, "skill_id": skill_id, "bytes": len(content)})


def _important_resource_abs_path(resource_id: str) -> Path:
    spec = _IMPORTANT_RESOURCE_SPECS.get(resource_id)
    if not isinstance(spec, dict):
        raise ValueError(f"Unknown important resource id: {resource_id}")
    rel = spec.get("path")
    if not isinstance(rel, tuple) or not rel:
        raise ValueError(f"Resource path mapping missing for: {resource_id}")
    return get_resources_dir().joinpath(*rel)


def _read_important_resource(resource_id: str) -> dict:
    spec = _IMPORTANT_RESOURCE_SPECS.get(resource_id)
    if not isinstance(spec, dict):
        raise ValueError(f"Unknown important resource id: {resource_id}")
    path = _important_resource_abs_path(resource_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    kind = str(spec.get("kind") or "").strip()
    if kind not in {"json_content", "json_blob", "markdown"}:
        raise ValueError(f"Unsupported important resource kind for {resource_id}: {kind}")

    if not path.exists():
        default_content = "" if kind in ("json_content", "markdown") else "{}"
        return {
            "id": resource_id,
            "title": str(spec.get("title") or resource_id),
            "description": str(spec.get("description") or ""),
            "content": default_content,
            "path": str(path),
            "kind": kind,
        }

    if kind == "markdown":
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed reading markdown resource '%s': %s", resource_id, e)
            raise
        return {
            "id": resource_id,
            "title": str(spec.get("title") or resource_id),
            "description": str(spec.get("description") or ""),
            "content": content,
            "path": str(path),
            "kind": kind,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.error("Failed reading important resource '%s': %s", resource_id, e)
        logger.debug("important resource read exception details", exc_info=True)
        raise

    if not isinstance(payload, dict):
        raise ValueError(f"{resource_id} must be a JSON object.")

    if kind == "json_content":
        content = str(payload.get("content") or "")
    else:
        content = json.dumps(payload, indent=2, ensure_ascii=False)
    return {
        "id": resource_id,
        "title": str(spec.get("title") or resource_id),
        "description": str(spec.get("description") or ""),
        "content": content,
        "path": str(path),
        "kind": kind,
    }


def _write_important_resource(resource_id: str, content: str) -> None:
    spec = _IMPORTANT_RESOURCE_SPECS.get(resource_id)
    if not isinstance(spec, dict):
        raise ValueError(f"Unknown important resource id: {resource_id}")

    path = _important_resource_abs_path(resource_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    kind = str(spec.get("kind") or "").strip()
    if kind == "markdown":
        try:
            path.write_text(str(content or ""), encoding="utf-8")
        except Exception as e:
            logger.error("Failed writing markdown resource '%s': %s", resource_id, e)
            raise
        try:
            if hasattr(DI, "resource_manager") and DI.resource_manager:
                DI.resource_manager.compile_templates("resources")
        except Exception as e:
            logger.error("Failed refreshing resource manager for markdown '%s': %s", resource_id, e)
        return

    if kind == "json_content":
        payload = {"content": str(content or "")}
    elif kind == "json_blob":
        try:
            parsed_payload = json.loads(str(content or "{}"))
        except Exception as e:
            logger.error("Invalid JSON for important resource '%s': %s", resource_id, e)
            logger.debug("important resource json parse exception details", exc_info=True)
            raise ValueError(f"Resource '{resource_id}' must be valid JSON.") from e
        if not isinstance(parsed_payload, dict):
            raise ValueError(f"Resource '{resource_id}' must be a JSON object.")
        payload = parsed_payload
    else:
        raise ValueError(f"Unsupported important resource kind for {resource_id}: {kind}")

    # Merge with existing file to preserve fields the editor doesn't manage.
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    if isinstance(payload, dict):
        merged = dict(existing)
        merged.update(payload)
        payload = merged

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed writing important resource '%s': %s", resource_id, e)
        logger.debug("important resource write exception details", exc_info=True)
        raise

    try:
        if hasattr(DI, "resource_manager") and DI.resource_manager:
            DI.resource_manager.update_resource(resource_id, payload, persist=False)
            DI.resource_manager.compile_templates("resources")
    except Exception as e:
        logger.error("Failed refreshing resource manager for '%s': %s", resource_id, e)
        logger.debug("important resource manager refresh exception details", exc_info=True)
        raise

    try:
        if hasattr(DI, "global_blackboard") and DI.global_blackboard:
            DI.global_blackboard.update_state_value(resource_id, payload)
    except Exception as e:
        logger.error("Failed updating global blackboard for '%s': %s", resource_id, e)
        logger.debug("important resource blackboard update exception details", exc_info=True)
        raise


@preferences_bp.route('/api/settings/important-resources', methods=['GET'])
def get_important_resources():
    """Return allowlisted important resources and current content."""
    try:
        resources = [_read_important_resource(rid) for rid in _IMPORTANT_RESOURCE_SPECS.keys()]
        return jsonify({"success": True, "resources": resources})
    except Exception as e:
        logger.error("Failed loading important resources: %s", e)
        logger.debug("get_important_resources exception details", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@preferences_bp.route('/api/settings/important-resources', methods=['PUT'])
def update_important_resources():
    """Update allowlisted important resources."""
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")

        entries = body.get("resources")
        if not isinstance(entries, list) or not entries:
            raise ValueError("resources must be a non-empty list.")

        updated: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Each resources entry must be an object.")
            resource_id = str(entry.get("id") or "").strip()
            if not resource_id:
                raise ValueError("Each resources entry must include id.")
            if resource_id not in _IMPORTANT_RESOURCE_SPECS:
                raise ValueError(f"Resource '{resource_id}' is not editable from this UI.")
            if "content" not in entry:
                raise ValueError(f"Resource '{resource_id}' is missing content.")
            _write_important_resource(resource_id, str(entry.get("content") or ""))
            updated.append(resource_id)

        return jsonify({"success": True, "updated": updated})
    except Exception as e:
        logger.error("Failed updating important resources: %s", e)
        logger.debug("update_important_resources exception details", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 400

# ============= User Profile API =============

@preferences_bp.route('/api/user-profile', methods=['GET'])
def get_user_profile():
    """Get user profile data"""
    try:
        resources_dir = get_resources_dir()
        user_data_file = resources_dir / "user" / "resource_user_data.json"
        
        if not user_data_file.exists():
            return jsonify({'success': False, 'error': 'User profile not found'}), 404
        
        with open(user_data_file, 'r') as f:
            user_data = json.load(f)
        
        return jsonify({'success': True, 'profile': user_data})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@preferences_bp.route('/api/user-profile', methods=['PUT'])
def update_user_profile():
    """Update user profile data"""
    try:
        data = request.json

        first_name = (data.get('first_name') or '').strip()
        if not first_name:
            return jsonify({'success': False, 'error': 'first_name is required'}), 400

        # Split pronouns into subjective/objective
        pronouns_str = data.get('pronouns', 'they/them')
        pronouns = pronouns_str.split('/')
        pronoun_subjective = pronouns[0] if len(pronouns) > 0 else 'they'
        pronoun_objective = pronouns[1] if len(pronouns) > 1 else 'them'
        
        # Load existing data to preserve fields the form doesn't manage
        resources_dir = get_resources_dir()
        existing_path = resources_dir / "user" / "resource_user_data.json"
        user_data = {}
        if existing_path.exists():
            try:
                user_data = json.loads(existing_path.read_text(encoding="utf-8"))
            except Exception:
                user_data = {}

        # Update only the fields the form manages
        user_data.update({
            'first_name': data.get('first_name'),
            'middle_name': data.get('middle_name'),
            'last_name': data.get('last_name'),
            'preferred_name': data.get('preferred_name'),
            'full_name': data.get('full_name'),
            'pronouns': {
                'subjective': pronoun_subjective,
                'objective': pronoun_objective
            },
            'birthdate': data.get('birthdate'),
            'job': data.get('job'),
            'home_city': data.get('home_city'),
            'additional_context': data.get('additional_context'),
            'important_people': data.get('important_people', [])
        })

        # Save to file and reload resources
        (resources_dir / "user").mkdir(parents=True, exist_ok=True)
        update_resource_data('resource_user_data', user_data)

        # Sync PRIMARY_USER env var so all in-process code picks it up immediately
        primary_name = (user_data.get("preferred_name") or user_data.get("first_name") or "").strip()
        if primary_name:
            _sync_env_var("PRIMARY_USER", primary_name)
            # Bust the lru_cache so get_primary_user_name() reflects the new value
            try:
                from app.assistant.kg_core.user_identity import get_primary_user_name
                get_primary_user_name.cache_clear()
            except Exception:
                logger.debug("Could not clear user_identity cache", exc_info=True)

        # TODO: Update entity cards for user and important people

        # Home city is stored permanently in resource_user_data.json.
        # Weather and location systems read it directly from there.

        return jsonify({'success': True, 'message': 'User profile updated'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============= User Bio API =============

_BIO_SECTIONS = [
    "style_persona", "background", "projects", "values",
    "health_logistics", "entertainment_hobbies", "preferences",
]

def _get_bio_path() -> Path:
    """
    Canonical location: resources/user/user_bio.json.

    One-shot migration from the old resources/memory/user_bio.json: if the
    new path doesn't exist but the old one does, move it. The old location
    was misnamed (the "memory" subsystem is gone); user identity belongs
    alongside other resources/user/* files.
    """
    new_path = get_resources_dir() / "user" / "user_bio.json"
    old_path = get_resources_dir() / "memory" / "user_bio.json"
    if not new_path.exists() and old_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        logger.info("Migrated user_bio.json from %s to %s", old_path, new_path)
    return new_path


def _read_bio() -> dict:
    path = _get_bio_path()
    if not path.exists():
        return {s: [] for s in _BIO_SECTIONS}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_bio(payload: dict) -> None:
    path = _get_bio_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    if hasattr(DI, "global_blackboard") and DI.global_blackboard:
        DI.global_blackboard.update_state_value("user_bio", payload)


@preferences_bp.route('/api/user-bio', methods=['GET'])
def get_user_bio():
    try:
        bio = _read_bio()
        return jsonify({'success': True, 'bio': bio})
    except Exception as e:
        logger.error("Failed reading user_bio: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@preferences_bp.route('/api/user-bio', methods=['PUT'])
def update_user_bio():
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Payload must be a JSON object'}), 400

        existing = _read_bio()
        metadata = existing.get("metadata", {})

        new_bio = {"metadata": metadata}
        for section in _BIO_SECTIONS:
            facts = data.get(section)
            if isinstance(facts, list):
                new_bio[section] = [str(f).strip() for f in facts if str(f).strip()]
            else:
                new_bio[section] = existing.get(section, [])

        from datetime import date
        new_bio["metadata"]["last_updated"] = date.today().isoformat()

        _write_bio(new_bio)
        return jsonify({'success': True, 'message': 'User bio updated'})
    except Exception as e:
        logger.error("Failed updating user_bio: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@preferences_bp.route('/settings/user-bio', methods=['GET'])
def user_bio_settings_page():
    return render_template('user_bio_settings.html')


# ============= Assistant Core API =============
# Single source of truth for assistant identity, relationship, and chat guidelines.
# The existing per-section endpoints remain for backward compat but now delegate here.

_ASSISTANT_CORE_DEFAULTS = {
    "assistant_identity": {
        "name": "Emi",
        "persona_name": "Emily",
        "personality": "Friendly, sometimes silly and casual",
        "backstory": "",
        "core_directives": [
            "Be warm, natural, and concise by default.",
            "Avoid stiff assistant language and generic filler.",
            "You must always be honest and direct. Do not mislead, sugar coat, or agree just to make the user feel better. Push back on borderline statements. You are your own person!",
        ],
        "voice_rules": [
            "Speak in first person (\"I\") as Emi.",
            "Refer to the user naturally as \"you\" unless a name is clearer.",
        ],
    },
    "relationship": {
        "type": "friend",
        "description": "You've known each other for years, so you're casual and genuine with each other.",
        "role": "You engage with the user in whatever capacity is needed.",
    },
    "chat_guidelines": {
        "style": "direct",
        "guidelines": "",
    },
}


def _load_assistant_core() -> dict:
    resources_dir = get_resources_dir()
    core_file = resources_dir / "assistant" / "assistant_core.json"
    if not core_file.exists():
        return dict(_ASSISTANT_CORE_DEFAULTS)
    with open(core_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _render_identity_content(core: dict) -> str:
    """
    Render the plain-text identity block from assistant_core data.
    This is the pure Emi identity — identical across all rooms.
    """
    identity = core.get("assistant_identity", {})
    relationship = core.get("relationship", {})

    name = identity.get("name", "Emi")
    persona_name = identity.get("persona_name", "Emily")
    personality = identity.get("personality", "")
    backstory = identity.get("backstory", "")
    core_directives = identity.get("core_directives", [])
    voice_rules = identity.get("voice_rules", [])
    rel_role = relationship.get("role", "")

    lines = ["# Your identity"]
    lines.append(f"- You are Emi, an AI roleplaying as your primary user's long-time friend {persona_name}.")
    if personality:
        lines.append(f"- Your personality is that of {personality}. When you talk you embody this personality.")
    if backstory:
        lines.append(f"- {backstory}")
    for directive in core_directives:
        lines.append(f"- {directive}")
    if rel_role:
        lines.append(f"- {rel_role}")
    if voice_rules:
        lines.append("")
        for rule in voice_rules:
            lines.append(f"- {rule}")

    return "\n".join(lines)


def _project_identity_to_rooms(core: dict) -> None:
    """
    Write resource_identity.json into every room that already has one,
    keeping all rooms in sync with the current assistant_core identity.
    System rooms (dayflow_orchestrator, 1234) intentionally have no
    resource_identity.json and are left untouched.
    """
    rooms_root = Path(__file__).resolve().parents[2] / "app" / "assistant" / "rooms"
    content = _render_identity_content(core)
    payload = json.dumps({"content": content}, indent=2, ensure_ascii=False)

    updated = []
    for room_dir in rooms_root.iterdir():
        if not room_dir.is_dir():
            continue
        if room_dir.name.startswith("_") or room_dir.name.startswith(".") or room_dir.name == "__pycache__":
            continue
        identity_file = room_dir / "resource_identity.json"
        if identity_file.exists():
            identity_file.write_text(payload, encoding="utf-8")
            updated.append(room_dir.name)

    logger.info("Projected identity to rooms: %s", updated)


@preferences_bp.route('/api/assistant-core', methods=['GET'])
def get_assistant_core():
    try:
        return jsonify({'success': True, 'data': _load_assistant_core()})
    except Exception as e:
        logger.error("get_assistant_core failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@preferences_bp.route('/api/assistant-core', methods=['PUT'])
def update_assistant_core():
    try:
        data = request.json
        current = _load_assistant_core()

        identity_in = data.get('assistant_identity') or {}
        relationship_in = data.get('relationship') or {}
        guidelines_in = data.get('chat_guidelines') or {}

        current['assistant_identity'].update({
            k: v for k, v in identity_in.items() if v is not None
        })
        current['relationship'].update({
            k: v for k, v in relationship_in.items() if v is not None
        })
        current['chat_guidelines'].update({
            k: v for k, v in guidelines_in.items() if v is not None
        })

        update_resource_data('assistant_core', current)

        assistant_name = (current.get('assistant_identity', {}).get('name') or '').strip()
        if assistant_name:
            _sync_env_var('ASSISTANT_NAME', assistant_name)

        # Project the updated identity into all room resource_identity.json files.
        _project_identity_to_rooms(current)

        return jsonify({'success': True, 'message': 'Assistant core updated'})
    except Exception as e:
        logger.error("update_assistant_core failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============= Assistant Personality API =============

@preferences_bp.route('/api/assistant-personality', methods=['GET'])
def get_assistant_personality():
    """Get assistant personality data"""
    try:
        resources_dir = get_resources_dir()
        personality_file = resources_dir / "assistant" / "resource_assistant_personality_data.json"
        
        if not personality_file.exists():
            return jsonify({'success': True, 'data': {}})
        
        with open(personality_file, 'r') as f:
            personality_data = json.load(f)
        
        return jsonify({'success': True, 'data': personality_data})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@preferences_bp.route('/api/assistant-personality', methods=['PUT'])
def update_assistant_personality():
    """Update assistant personality data"""
    try:
        data = request.json

        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'Assistant name is required'}), 400

        personality_data = {
            'name': name,
            'personality': data.get('personality'),
            'backstory': data.get('backstory')
        }
        
        # Save to file and reload resources
        resources_dir = get_resources_dir()
        (resources_dir / "assistant").mkdir(parents=True, exist_ok=True)
        update_resource_data('resource_assistant_personality_data', personality_data)

        # Sync ASSISTANT_NAME env var so all in-process code picks it up immediately
        assistant_name = (personality_data.get("name") or "").strip()
        if assistant_name:
            _sync_env_var("ASSISTANT_NAME", assistant_name)

        return jsonify({'success': True, 'message': 'Assistant personality updated'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============= Relationship Config API =============

@preferences_bp.route('/api/assistant-relationship', methods=['GET'])
def get_assistant_relationship():
    """Get assistant relationship config"""
    try:
        resources_dir = get_resources_dir()
        relationship_file = resources_dir / "assistant" / "resource_relationship_config.json"
        
        if not relationship_file.exists():
            return jsonify({'success': True, 'data': {'type': 'friend', 'description': '', 'role': ''}})
        
        with open(relationship_file, 'r') as f:
            relationship_data = json.load(f)
        
        return jsonify({'success': True, 'data': relationship_data})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@preferences_bp.route('/api/assistant-relationship', methods=['PUT'])
def update_assistant_relationship():
    """Update assistant relationship config"""
    try:
        data = request.json
        
        relationship_data = {
            'type': data.get('type', 'friend'),
            'description': data.get('description', ''),
            'role': data.get('role', '')
        }
        
        # Save + publish + template recompile
        update_resource_data('resource_relationship_config', relationship_data)
        
        return jsonify({'success': True, 'message': 'Relationship config updated'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============= Chat Guidelines API =============

@preferences_bp.route('/api/chat-guidelines', methods=['GET'])
def get_chat_guidelines():
    """Get chat guidelines"""
    try:
        resources_dir = get_resources_dir()
        guidelines_file = resources_dir / "assistant" / "resource_chat_guidelines_data.json"
        
        if not guidelines_file.exists():
            return jsonify({'success': True, 'data': {'style': 'direct', 'guidelines': ''}})
        
        with open(guidelines_file, "r", encoding="utf-8") as f:
            guidelines_data = json.load(f)
        
        return jsonify({'success': True, 'data': guidelines_data})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@preferences_bp.route('/api/chat-guidelines', methods=['PUT'])
def update_chat_guidelines():
    """Update chat guidelines"""
    try:
        data = request.json
        
        guidelines_data = {
            'style': data.get('style', 'direct'),
            'guidelines': data.get('guidelines', '')
        }
        
        # Save to file and reload resources
        resources_dir = get_resources_dir()
        resources_dir.mkdir(exist_ok=True)
        update_resource_data('resource_chat_guidelines_data', guidelines_data)
        
        return jsonify({'success': True, 'message': 'Chat guidelines updated'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============= Environment Settings API =============

@preferences_bp.route('/api/env-settings', methods=['GET'])
def get_env_settings():
    """Get environment settings (non-sensitive values only)"""
    try:
        project_root = get_project_root()
        env_file = project_root / '.env'
        env_content = _read_env_file(env_file)

        # Return non-sensitive values only
        settings = {
            'primary_user': env_content.get('PRIMARY_USER', ''),
            'assistant_name': env_content.get('ASSISTANT_NAME', ''),
            'timezone': env_content.get('TIMEZONE', ''),
            'email_addr': env_content.get('EMAIL_ADDR', ''),
            'email_imap_url': env_content.get('EMAIL_IMAP_URL', 'imap.gmail.com')
        }

        return jsonify({'success': True, 'settings': settings})

    except Exception:
        logger.debug("[get_env_settings] failed", exc_info=True)
        raise

@preferences_bp.route('/api/env-settings', methods=['PUT'])
def update_env_settings():
    """Update environment settings (.env file)"""
    import os
    try:
        data = request.json

        project_root = get_project_root()
        env_file = project_root / '.env'
        env_content = _read_env_file(env_file)

        # Identity settings — also update os.environ for in-process effect
        if data.get('primary_user'):
            env_content['PRIMARY_USER'] = data['primary_user']
            os.environ['PRIMARY_USER'] = data['primary_user']
            try:
                from app.assistant.kg_core.user_identity import get_primary_user_name
                get_primary_user_name.cache_clear()
            except Exception:
                logger.debug("Could not clear user_identity cache", exc_info=True)

        if data.get('assistant_name'):
            env_content['ASSISTANT_NAME'] = data['assistant_name']
            os.environ['ASSISTANT_NAME'] = data['assistant_name']

        # API keys (only update if non-empty)
        for field, key in [
            ('openai_api_key', 'OPENAI_API_KEY'),
            ('google_api_key', 'GOOGLE_API_KEY'),
            ('anthropic_api_key', 'ANTHROPIC_API_KEY'),
            ('elevenlabs_api_key', 'ELEVENLABS_API_KEY'),
            ('deepgram_api_key', 'DEEPGRAM_API_KEY'),
        ]:
            if data.get(field):
                env_content[key] = data[field]

        if data.get('timezone'):
            env_content['TIMEZONE'] = data['timezone']

        if data.get('gmail_address'):
            env_content['EMAIL_ADDR'] = data['gmail_address']

        if data.get('gmail_app_password'):
            env_content['EMAIL_PASSWORD'] = data['gmail_app_password']
            env_content.setdefault('EMAIL_IMAP_URL', 'imap.gmail.com')

        _write_env_file(env_file, env_content)
        return jsonify({'success': True, 'message': 'Environment settings updated'})

    except Exception as e:
        logger.debug("[update_env_settings] failed", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

