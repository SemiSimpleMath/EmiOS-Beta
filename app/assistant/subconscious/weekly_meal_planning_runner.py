"""Canonical weekly meal-planning chain — the single weekly path.

    seed -> meal_context_distiller -> weekly_meal_planning_manager
    (delegator -> planner -> [meal_research] -> weekly_meal_planner) -> recover -> persist

Shared by ALL three callers: the Sunday cron (routine_handlers/subconscious.
weekly_meal_planner_run), the /meals "Generate" button (meal_page_service.
generate_plan_for_week), and the CLI (run_weekly_meal_planning).

Before this module existed, the cron called the `weekly_meal_planner` agent DIRECTLY with
the raw seed — but that agent's prompt only reads `meal_context.*`, which is produced by
the distiller. So the autonomous Sunday plan was blind to dietary restrictions, learned
beliefs, concerns, and per-day audience; only the manual /meals button (which runs the
distiller) got them. Routing all three callers through this one function fixes that and
removes the duplicated orchestration. See scratch/MEAL-PLANNING-AUDIT.md §6.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.subconscious.meal_context_builder import build_weekly_meal_planner_context
from app.assistant.subconscious.meal_persist import apply_weekly_meal_planner_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


def upcoming_monday_iso() -> str:
    """ISO date of the NEXT Monday strictly after today — the week the Sunday-evening run
    plans. (Sun -> tomorrow; Sat -> +2; if run on a Monday -> the following Monday.)"""
    now_local = get_local_time()
    days_ahead = (0 - now_local.weekday()) % 7  # Mon=0 -> 0, Sun=6 -> 1, Sat=5 -> 2
    days_ahead = days_ahead or 7                 # today IS Monday -> plan NEXT Monday
    return (now_local + timedelta(days=days_ahead)).date().isoformat()


def _ensure_manager_runtime_registered() -> None:
    """mam_instance_manager is registered by app bootstrap in the live app; the standalone
    CLI / test_setup path doesn't register it. No-op when already present."""
    if not hasattr(DI, "mam_instance_manager") or DI.mam_instance_manager is None:
        from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
        ServiceLocator.register(
            "mam_instance_manager",
            MAMInstanceManager(resource_manager=getattr(DI, "resource_manager", None)),
        )


def _recover_weekly_meal_planner_output(manager) -> Dict[str, Any]:
    """Pull the weekly_meal_planner agent's structured output off the manager blackboard.
    Iterate in REVERSE so a multi-cycle run returns the LATEST emission, not the first."""
    bb = getattr(manager, "blackboard", None)
    if bb is None:
        return {}
    try:
        msgs = list(bb.get_messages())
    except Exception as e:
        logger.warning("[weekly_meal_planning] blackboard read failed: %s", e)
        return {}
    for m in reversed(msgs):
        if str(getattr(m, "sender", "") or "") == "weekly_meal_planner":
            data = getattr(m, "data", None) or {}
            if isinstance(data, dict) and data:
                return data
    return {}


def run_weekly_meal_planning_chain(
    week_start: Optional[str] = None, *, persist: bool = True
) -> Dict[str, Any]:
    """Run the full weekly chain for ``week_start`` (ISO Monday; ``None`` uses the current
    week's Monday from the seed) and optionally persist the plan pod + weekly shopping doc.

    Returns the persist summary (incl. ``plan_pod_id``) plus ``week_start_date`` and
    ``meal_context_days``. Fails LOUD — any stage failure raises (callers that need a
    graceful response, e.g. the /meals route, catch it).
    """
    seed = build_weekly_meal_planner_context()
    if week_start:
        # Align the distiller's days[] AND the resulting plan to the target week.
        seed["week_start_date"] = week_start
    week_iso = seed["week_start_date"]
    seed["horizon_days"] = "7"

    _ensure_manager_runtime_registered()

    # Stage 1 — distiller: raw seed -> the `meal_context` bundle the planner prompt reads
    # (horizon_summary / relevant_beliefs / horizon_wide_concerns / inventory_pressures /
    # days[] / additional_notes). This is the step the old cron path skipped.
    distiller = DI.agent_factory.create_agent("meal_context_distiller")
    if distiller is None:
        raise RuntimeError("weekly_meal_planning_chain: meal_context_distiller unavailable")
    distiller_scope = load_scope_for_source(
        kind="subsystem", source_id="subconscious", actor_id="weekly_meal_planning_chain",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::meal_context_distiller", "surface": "internal"},
    )
    distiller_result = distiller.action_handler(
        Message(agent_input=seed, scope_context=distiller_scope)
    )
    meal_context: Dict[str, Any] = (
        distiller_result.data if isinstance(getattr(distiller_result, "data", None), dict) else {}
    )

    # Stage 2 — planning manager: delegator -> planner -> [meal_research] -> weekly_meal_planner.
    manager = DI.multi_agent_manager_factory.create_manager("weekly_meal_planning_manager")
    if manager is None:
        raise RuntimeError("weekly_meal_planning_chain: weekly_meal_planning_manager unavailable")
    mgr_scope = load_scope_for_source(
        kind="subsystem", source_id="subconscious", actor_id="weekly_meal_planning_chain",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::weekly_meal_planning_manager", "surface": "internal"},
    )
    manager_context: Dict[str, Any] = {
        "date_time": seed.get("date_time"),
        "day_of_week": seed.get("day_of_week"),
        "week_start_date": week_iso,
        "meal_context": meal_context,
        # 4-week history (variety + cadence) + shopping-list mechanics (raw).
        "recent_planned_meals": seed.get("recent_planned_meals"),
        "inventory_snapshot": seed.get("inventory_snapshot"),
        "ralphs_standing_list": seed.get("ralphs_standing_list"),
        "agent_weekly_list_state": seed.get("agent_weekly_list_state"),
    }
    bb = manager.blackboard
    for k, v in manager_context.items():
        bb.update_state_value(k, v)
    DI.manager_invoker.invoke(manager, Message(agent_input=manager_context, scope_context=mgr_scope))

    output = _recover_weekly_meal_planner_output(manager)
    if not output:
        raise RuntimeError("weekly_meal_planning_chain: no weekly_meal_planner output recovered")

    summary: Dict[str, Any] = {
        "week_start_date": week_iso,
        "meal_context_days": len(meal_context.get("days") or []),
    }
    if persist:
        summary.update(apply_weekly_meal_planner_output(output) or {})
    else:
        summary["persisted"] = False
        summary["output"] = output
    return summary
