# health_check.py
"""
Health check endpoint to diagnose app state issues.
Useful for debugging overnight state corruption.
"""
from flask import Blueprint, jsonify, current_app
from datetime import datetime, timezone
import signal
import sys
import os

from app.routes._security import local_only

health_check_bp = Blueprint('health_check', __name__)


def _event_repo_counts():
    """Return (counts, healthy, error) from the event repository. The per-category COUNTS are internal
    detail (admin-only); the healthy bool is safe to expose. Used by both /health (bool only) and the
    gated /api/system/health (full counts)."""
    import json
    from app.assistant.event_repository.event_repository import EventRepositoryManager
    event_repo = EventRepositoryManager()
    categories = ["calendar", "scheduler", "email", "weather", "todo_task", "news"]
    counts, healthy, error = {}, True, None
    for category in categories:
        try:
            counts[category] = len(json.loads(event_repo.search_events(data_type=category)))
        except Exception as e:
            counts[category] = f"ERROR: {str(e)}"
            healthy = False
            error = str(e)
    return counts, healthy, error


@health_check_bp.route('/health', methods=['GET'])
def health_check():
    """Public liveness probe — intentionally LEAN. It reports only whether the app is up and the DB is
    reachable. Internal detail (python_version, per-category DB counts, memory, scheduler/loop health)
    is admin-only and lives behind the local-only /api/system/health — leaking it to any caller is a
    recon aid with no upside for an unauthenticated probe.
    """
    try:
        _counts, db_healthy, _db_error = _event_repo_counts()
        return jsonify({
            "status": "healthy" if db_healthy else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database_reachable": db_healthy,
        }), 200 if db_healthy else 503
    except Exception as e:
        current_app.logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


@health_check_bp.route('/api/system/health', methods=['GET'])
@local_only
def system_health():
    """Local-only deep health surface (reliability R4) — makes the spine's signals observable.

    Aggregates: per-loop heartbeats (the R3 SSOT: dayflow tick + every background task), background-
    task thread liveness, the DB writer's serialization stats, and process info. Each section is read
    independently and any failure is REPORTED in the body (not hidden) — for a health endpoint,
    surfacing 'this subsystem is unreadable' IS failing loud. Gated to loopback (never over the
    tunnel) because it exposes internal operational detail; the public /health stays a liveness probe.
    """
    out = {"timestamp": datetime.now(timezone.utc).isoformat()}
    degraded_reasons = []

    # Per-loop heartbeats — dayflow tick + all background tasks (the R3 single source of truth).
    try:
        from app.services.scheduler_heartbeat import get_all as _heartbeats
        heartbeats = _heartbeats()
        out["heartbeats"] = heartbeats
        for name, hb in heartbeats.items():
            if hb.get("consecutive_errors", 0) > 0:
                degraded_reasons.append(f"{name}: {hb['consecutive_errors']} consecutive errors")
    except Exception as e:
        out["heartbeats"] = {"error": str(e)}
        degraded_reasons.append("heartbeats unavailable")

    # Background-task thread liveness — a thread can die without ticking, which heartbeats can't show.
    try:
        from app.assistant.background_task_manager.background_task_manager import (
            get_background_task_manager,
        )
        bt = get_background_task_manager().get_status()
        out["background_tasks"] = bt
        for name, st in (bt.get("tasks") or {}).items():
            if st.get("should_be_running") and not st.get("thread_alive"):
                degraded_reasons.append(f"background task '{name}' is not alive")
    except Exception as e:
        out["background_tasks"] = {"error": str(e)}

    # DB writer serialization stats (in-memory counters — cheap).
    try:
        from app.models.db_manager import get_db_manager
        out["db_writer"] = get_db_manager().stats()
    except Exception as e:
        out["db_writer"] = {"error": str(e)}

    # Per-category event counts — the internal DB detail that used to leak from the public /health.
    try:
        counts, db_healthy, db_error = _event_repo_counts()
        out["database"] = {"healthy": db_healthy, "error": db_error, "event_counts": counts}
        if not db_healthy:
            degraded_reasons.append("event repository unhealthy")
    except Exception as e:
        out["database"] = {"error": str(e)}

    try:
        import psutil
        process = psutil.Process(os.getpid())
        start_time = datetime.fromtimestamp(process.create_time(), tz=timezone.utc)
        out["process"] = {
            "uptime_hours": round((datetime.now(timezone.utc) - start_time).total_seconds() / 3600, 2),
            "memory_mb": round(process.memory_info().rss / 1024 / 1024, 2),
            "pid": os.getpid(),
            "python_version": sys.version,
        }
    except Exception as e:
        out["process"] = {"error": str(e)}

    out["status"] = "degraded" if degraded_reasons else "ok"
    out["degraded_reasons"] = degraded_reasons
    return jsonify(out), 200


@health_check_bp.route('/api/shutdown', methods=['POST'])
@local_only
def shutdown():
    """Gracefully shut down the server from the UI (local only — never over a tunnel)."""
    current_app.logger.info("Shutdown requested from UI")
    os.kill(os.getpid(), signal.SIGTERM)
    return jsonify({"status": "shutting_down"}), 200


@health_check_bp.route('/api/version', methods=['GET'])
def version():
    """Installed application version — the tray updater and the 'About' UI read
    this to compare against the latest GitHub release."""
    from app import __version__
    return jsonify({"version": __version__}), 200

