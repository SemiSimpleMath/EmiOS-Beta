"""Routines admin UI + management API.

Renders /routines (hour-by-hour schedule + table) and exposes endpoints
to toggle enabled, edit run_policy fields, and trigger run-now. Writes
back to configs/routines.json atomically; the routine_manager re-reads
config on every refresh tick so edits apply at the next tick.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from app.assistant.routine_manager import get_routine_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir, get_resources_dir
from app.assistant.utils.time_utils import get_local_time, to_rfc3339_z, utc_now

logger = get_logger(__name__)

routines_admin_bp = Blueprint("routines_admin", __name__)

_CONFIG_LOCK = threading.Lock()
_CONFIG_FILE = "routines.json"
_STATE_FILE = "resource_routine_status.json"


def _config_path() -> Path:
    return get_configs_dir() / _CONFIG_FILE


def _state_path() -> Path:
    return get_resources_dir() / _STATE_FILE


def _load_config() -> Dict[str, Any]:
    p = _config_path()
    if not p.exists():
        return {"routines": []}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(config: Dict[str, Any]) -> None:
    """Atomic write: tmp file → fsync → replace."""
    p = _config_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
    tmp.replace(p)


def _load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"routines": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load routine state: %s", exc)
        return {"routines": {}}


def _save_state(state: Dict[str, Any]) -> None:
    """Atomic write of the runtime status file. Per-routine `enabled`
    lives here (the K8s-shape "status" half of the spec/status split):
    toggles are per-machine state, not declarative schema, so they
    must not commit-flip configs/routines.json."""
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
    tmp.replace(p)


def _find_routine(config: Dict[str, Any], routine_id: str) -> Optional[Dict[str, Any]]:
    for r in config.get("routines", []):
        if r.get("id") == routine_id:
            return r
    return None


def _format_interval(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m" if not s else f"{m}m{s}s"
    h = seconds / 3600
    return f"{h:.1f}h" if h != int(h) else f"{int(h)}h"


def _enrich_routine(item: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    rid = item.get("id")
    state_entry = (state.get("routines") or {}).get(rid) or {}
    rp = item.get("run_policy") or {}
    policy_type = (rp.get("type") or "").strip().lower()
    schedule_label = ""
    if policy_type == "daily":
        schedule_label = f"daily at {rp.get('time_local', '??:??')}"
    elif policy_type == "weekly":
        schedule_label = f"{rp.get('day_of_week', '?')} {rp.get('time_local', '??:??')}"
    elif policy_type == "interval":
        schedule_label = f"every {_format_interval(int(rp.get('min_interval_seconds') or 0))}"
    else:
        schedule_label = policy_type or "—"

    # Effective enabled: status override wins over spec default.
    # Same spec/status separation the routine_manager uses at scheduler time
    # — keeps the UI honest about what's actually running on this machine.
    if isinstance(state_entry, dict) and "enabled" in state_entry:
        effective_enabled = bool(state_entry["enabled"])
    else:
        effective_enabled = bool(item.get("enabled", False))

    return {
        "id": rid,
        "name": item.get("name") or rid,
        "enabled": effective_enabled,
        "runner": item.get("runner") or "—",
        "run_policy": rp,
        "policy_type": policy_type,
        "schedule_label": schedule_label,
        "notes": item.get("notes") or "",
        "feature_guard": item.get("feature_guard") or None,
        "afk_guard": item.get("afk_guard") or None,
        "manual_toggle": item.get("manual_toggle") or None,
        "last_run_utc": state_entry.get("last_run_utc"),
        "last_finished_utc": state_entry.get("last_finished_utc"),
        "last_status": state_entry.get("last_status"),
        "last_error": state_entry.get("last_error"),
        "last_duration_s": state_entry.get("last_duration_s"),
        "run_count": state_entry.get("run_count", 0),
    }


@routines_admin_bp.route("/routines", methods=["GET"])
def routines_page():
    return render_template("routines_admin.html")


@routines_admin_bp.route("/api/routines", methods=["GET"])
def routines_list():
    config = _load_config()
    state = _load_state()
    items = [_enrich_routine(r, state) for r in (config.get("routines") or [])]
    return jsonify({
        "now_local": get_local_time().isoformat(timespec="seconds"),
        "now_utc": to_rfc3339_z(utc_now().replace(microsecond=0)),
        "manager_enabled": bool(config.get("enabled", True)),
        "max_workers": config.get("max_workers", 0),
        "routines": items,
    })


@routines_admin_bp.route("/api/routines/<routine_id>/toggle", methods=["POST"])
def routines_toggle(routine_id: str):
    """Toggle a routine's effective `enabled` flag. Writes to the status
    file (resource_routine_status.json), NOT to the spec
    (configs/routines.json) — same K8s-shape spec/status separation:
    the schema is shared, the runtime state is per-machine. Lets users
    pull schema updates from upstream without losing their toggle state,
    and lets them commit schema improvements without pushing personal
    on/off flags into the public repo.
    """
    payload = request.get_json(silent=True) or {}
    desired = payload.get("enabled")
    with _CONFIG_LOCK:
        config = _load_config()
        r = _find_routine(config, routine_id)
        if r is None:
            return jsonify({"ok": False, "error": f"routine not found: {routine_id}"}), 404

        state = _load_state()
        if not isinstance(state.get("routines"), dict):
            state["routines"] = {}
        existing = state["routines"].get(routine_id) or {}
        if not isinstance(existing, dict):
            existing = {}
        # Current effective enabled = status override if set, else spec default.
        current_enabled = (
            bool(existing["enabled"])
            if "enabled" in existing
            else bool(r.get("enabled", False))
        )
        new_enabled = (not current_enabled) if desired is None else bool(desired)
        existing["enabled"] = new_enabled
        state["routines"][routine_id] = existing
        _save_state(state)
    logger.info("[routines_admin] %s enabled=%s (status override)", routine_id, new_enabled)
    return jsonify({"ok": True, "id": routine_id, "enabled": new_enabled})


_VALID_POLICY_KEYS = {"time_local", "day_of_week", "min_interval_seconds"}
_VALID_DOW = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _validate_policy_patch(rp_existing: Dict[str, Any], patch: Dict[str, Any]) -> Optional[str]:
    policy_type = (rp_existing.get("type") or "").strip().lower()
    for k in patch.keys():
        if k not in _VALID_POLICY_KEYS:
            return f"unsupported policy field: {k}"
    if "time_local" in patch:
        v = str(patch["time_local"]).strip()
        try:
            hh, mm = v.split(":")
            if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError
            patch["time_local"] = f"{int(hh):02d}:{int(mm):02d}"
        except Exception:
            return f"invalid time_local: {v} (expected HH:MM 24h)"
        if policy_type not in {"daily", "weekly"}:
            return f"time_local only valid for daily/weekly (got {policy_type})"
    if "day_of_week" in patch:
        v = str(patch["day_of_week"]).strip().capitalize()
        if v not in _VALID_DOW:
            return f"invalid day_of_week: {v}"
        patch["day_of_week"] = v
        if policy_type != "weekly":
            return f"day_of_week only valid for weekly (got {policy_type})"
    if "min_interval_seconds" in patch:
        try:
            n = int(patch["min_interval_seconds"])
        except Exception:
            return "min_interval_seconds must be an integer"
        if n < 30 or n > 86400 * 7:
            return "min_interval_seconds must be 30..604800"
        patch["min_interval_seconds"] = n
        if policy_type != "interval":
            return f"min_interval_seconds only valid for interval (got {policy_type})"
    return None


@routines_admin_bp.route("/api/routines/decisions", methods=["GET"])
def routines_decisions_recent():
    """Recent routine decisions across all routines (newest first).

    Query params:
      routine_id : optional, filter to one routine
      limit      : default 100, capped at 500
      days_back  : default 2, capped at 14
    """
    from app.assistant.routine_manager import decision_log
    routine_id = (request.args.get("routine_id") or "").strip() or None
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 100)))
    except ValueError:
        limit = 100
    try:
        days_back = max(1, min(14, int(request.args.get("days_back") or 2)))
    except ValueError:
        days_back = 2
    rows = decision_log.read_recent(routine_id=routine_id, limit=limit, days_back=days_back)
    return jsonify({"decisions": rows, "count": len(rows)})


@routines_admin_bp.route("/api/routines/<routine_id>/decisions", methods=["GET"])
def routines_decisions_for(routine_id: str):
    """Recent decisions for one routine. Convenience wrapper over the
    /api/routines/decisions?routine_id=X form."""
    from app.assistant.routine_manager import decision_log
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 50)))
    except ValueError:
        limit = 50
    try:
        days_back = max(1, min(14, int(request.args.get("days_back") or 7)))
    except ValueError:
        days_back = 7
    rows = decision_log.read_recent(routine_id=routine_id, limit=limit, days_back=days_back)
    return jsonify({"routine_id": routine_id, "decisions": rows, "count": len(rows)})


@routines_admin_bp.route("/api/routines/<routine_id>/policy", methods=["POST"])
def routines_policy_patch(routine_id: str):
    patch = request.get_json(silent=True) or {}
    if not patch:
        return jsonify({"ok": False, "error": "empty patch"}), 400
    with _CONFIG_LOCK:
        config = _load_config()
        r = _find_routine(config, routine_id)
        if r is None:
            return jsonify({"ok": False, "error": f"routine not found: {routine_id}"}), 404
        rp = r.setdefault("run_policy", {})
        err = _validate_policy_patch(rp, patch)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        rp.update(patch)
        _save_config(config)
    logger.info("[routines_admin] %s policy patched: %s", routine_id, patch)
    return jsonify({"ok": True, "id": routine_id, "run_policy": rp})


@routines_admin_bp.route("/api/routines/<routine_id>/run-now", methods=["POST"])
def routines_run_now(routine_id: str):
    rm = get_routine_manager()

    def _go():
        try:
            rm.run_routine_now(routine_id)
        except Exception as exc:
            logger.error("[routines_admin] run-now failed for %s: %s", routine_id, exc)

    threading.Thread(target=_go, name=f"routine-run-now-{routine_id}", daemon=True).start()
    return jsonify({"ok": True, "id": routine_id, "started": True})
