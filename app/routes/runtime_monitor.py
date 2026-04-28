from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.routine_manager import get_routine_manager
from app.assistant.runtime import get_runtime_registry
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

runtime_monitor_bp = Blueprint("runtime_monitor", __name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _build_summary(
    *,
    routine_status: dict[str, Any],
    manager_status: dict[str, Any],
    registry_status: dict[str, Any],
) -> dict[str, Any]:
    routine_saturation = _safe_float(routine_status.get("saturation_ratio"), 0.0)
    warn_ratio = _safe_float(((routine_status.get("thresholds") or {}).get("warning_ratio")), 0.8)
    critical_ratio = _safe_float(((routine_status.get("thresholds") or {}).get("critical_ratio")), 0.95)

    routine_level = "ok"
    if routine_saturation >= critical_ratio:
        routine_level = "critical"
    elif routine_saturation >= warn_ratio:
        routine_level = "warning"

    active_invocations = _safe_int(manager_status.get("active_invocation_count"), 0)
    longest_manager_running_s = 0.0
    for row in manager_status.get("active_invocations", []):
        if not isinstance(row, dict):
            continue
        longest_manager_running_s = max(longest_manager_running_s, _safe_float(row.get("running_for_s"), 0.0))

    executors = registry_status.get("executors") if isinstance(registry_status.get("executors"), list) else []
    registry_threads = registry_status.get("threads") if isinstance(registry_status.get("threads"), list) else []
    total_executor_running = 0
    max_executor_saturation = 0.0
    for row in executors:
        if not isinstance(row, dict):
            continue
        total_executor_running += _safe_int(row.get("running"), 0)
        max_executor_saturation = max(max_executor_saturation, _safe_float(row.get("saturation_ratio"), 0.0))
    registered_alive_threads = 0
    for row in registry_threads:
        if not isinstance(row, dict):
            continue
        if bool(row.get("is_alive", False)):
            registered_alive_threads += 1
    unregistered = registry_status.get("python_runtime_threads") if isinstance(registry_status.get("python_runtime_threads"), dict) else {}

    return {
        "routine_capacity_level": routine_level,
        "routine_saturation_ratio": routine_saturation,
        "active_manager_invocations": active_invocations,
        "longest_manager_running_s": round(longest_manager_running_s, 2),
        "registered_runtime_threads_alive": registered_alive_threads,
        "registered_executors": len(executors),
        "executor_tasks_running": total_executor_running,
        "max_executor_saturation_ratio": round(max_executor_saturation, 4),
        "python_unregistered_thread_count": _safe_int(unregistered.get("unregistered_count"), 0),
    }


@runtime_monitor_bp.route("/api/runtime/concurrency", methods=["GET"])
def runtime_concurrency_status():
    try:
        routine_manager = get_routine_manager()
        routine_status = routine_manager.get_runtime_concurrency_status()

        manager_invoker = getattr(DI, "manager_invoker", None)
        if manager_invoker is None:
            raise RuntimeError("manager_invoker is not initialized.")
        if not hasattr(manager_invoker, "get_invocation_status"):
            raise RuntimeError("manager_invoker does not expose get_invocation_status().")
        manager_status = manager_invoker.get_invocation_status()
        if not isinstance(manager_status, dict):
            raise RuntimeError("manager_invoker.get_invocation_status() returned non-dict payload.")

        runtime_registry = get_runtime_registry()
        registry_status = runtime_registry.snapshot()
        if not isinstance(registry_status, dict):
            raise RuntimeError("runtime_registry.snapshot() returned non-dict payload.")

        generated_at_utc = datetime.now(timezone.utc).isoformat()
        summary = _build_summary(
            routine_status=routine_status,
            manager_status=manager_status,
            registry_status=registry_status,
        )
        return jsonify(
            {
                "success": True,
                "generated_at_utc": generated_at_utc,
                "summary": summary,
                "routine_manager": routine_status,
                "manager_invoker": manager_status,
                "runtime_registry": registry_status,
            }
        )
    except Exception as e:
        logger.error("Failed to build runtime concurrency status: %s", e)
        logger.debug("runtime concurrency status exception details", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@runtime_monitor_bp.route("/debug/runtime/concurrency", methods=["GET"])
def runtime_concurrency_dashboard():
    return render_template("runtime_concurrency.html")

