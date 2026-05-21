"""CLI to run weekly_meal_planning_manager + persist the output.

Architecture today:
  1. (Optional, separate step) meal_context_distiller runs first to
     produce a focused per-day context. NOT wired into this runner yet
     — that's the next iteration. For now the manager reads the full
     seed directly.
  2. weekly_meal_planning_manager:
       delegator → weekly_meal_planning::planner (decide research vs
       proceed, biased toward proceed) → maybe call
       meal_research_manager → weekly_meal_planner (produce structured
       WeeklyMealPlan) → exit.
  3. meal_persist writes the plan pod + updates the weekly shopping doc.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning --no-persist
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning --week-start 2026-05-25
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator


def _ensure_manager_runtime_registered() -> None:
    """test_setup registers manager_invoker but not mam_instance_manager.
    Patch the gap the way the kg_investigator full-bootstrap path does.
    """
    if not hasattr(DI, "mam_instance_manager") or DI.mam_instance_manager is None:
        from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
        resource_manager = getattr(DI, "resource_manager", None)
        ServiceLocator.register(
            "mam_instance_manager",
            MAMInstanceManager(resource_manager=resource_manager),
        )


from app.assistant.subconscious.meal_context_builder import build_weekly_meal_planner_context
from app.assistant.subconscious.meal_persist import apply_weekly_meal_planner_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run weekly_meal_planning_manager.")
    parser.add_argument("--no-persist", action="store_true", help="Run manager but don't mint pods or write doc.")
    parser.add_argument("--week-start", type=str, default=None, help="Override week_start_date (YYYY-MM-DD).")
    args = parser.parse_args()

    print("=" * 70)
    print(f"WEEKLY MEAL PLANNING")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building seed context...")
    context = build_weekly_meal_planner_context()
    if args.week_start:
        context["week_start_date"] = args.week_start
        print(f"        week_start_date overridden to {args.week_start}")

    print("\n[2/4] Invoking weekly_meal_planning_manager...")
    _ensure_manager_runtime_registered()
    manager = DI.multi_agent_manager_factory.create_manager("weekly_meal_planning_manager")
    if manager is None:
        print("ERROR: failed to create weekly_meal_planning_manager.")
        sys.exit(1)
    scope = ScopeContext(
        scope_id="subconscious::weekly_meal_planning_manager",
        owner_id="system",
        actor_id="run_weekly_meal_planning",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    # Seed the manager's shared blackboard so both the planning::planner
    # and the downstream weekly_meal_planner can resolve user_context_items.
    bb = manager.blackboard
    for k, v in context.items():
        bb.update_state_value(k, v)
    msg = Message(agent_input=context, scope_context=scope)
    try:
        DI.manager_invoker.invoke(manager, msg)
    except Exception as e:
        print(f"ERROR: manager raised: {type(e).__name__}: {e}")
        logger.exception("weekly_meal_planning_manager raised")
        sys.exit(2)

    output = _recover_weekly_meal_planner_output(manager)

    print("\n[3/4] Output:")
    plan = output.get("weekly_plan") or {}
    print(f"      week_start_date: {plan.get('week_start_date')}")
    print(f"      theme: {plan.get('week_theme', '')[:200]}")
    anchors = plan.get("anchor_meals") or []
    print(f"      anchor_meals ({len(anchors)}): {', '.join(anchors)}")
    slots = plan.get("slots") or []
    from collections import Counter
    types = Counter(s.get("slot_type") for s in slots)
    print(f"      slots ({len(slots)}): {dict(types)}")
    print(f"      addressed_concern_ids: {output.get('addressed_concern_ids') or []}")
    weekly_list = output.get("weekly_shopping_list") or {}
    print(f"      weekly_shopping_list: action={weekly_list.get('action')} week_start={weekly_list.get('week_start_date')}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pods or writing doc.")
        return
    if not output:
        print("\n[4/4] No output recovered; skipping persist.")
        return
    print("\n[4/4] Persisting plan pod + updating weekly shopping doc...")
    persist_summary = apply_weekly_meal_planner_output(output)
    for k, v in persist_summary.items():
        if isinstance(v, list):
            print(f"      {k}: ({len(v)} items) {v[:5]}{'...' if len(v) > 5 else ''}")
        else:
            print(f"      {k}: {v}")
    print("\n" + "=" * 70 + "\nDONE\n" + "=" * 70)


def _recover_weekly_meal_planner_output(manager) -> Dict[str, Any]:
    """Pull weekly_meal_planner's structured output (the final_answer step
    of the manager) from the blackboard.
    """
    bb = getattr(manager, "blackboard", None)
    if bb is None:
        return {}
    try:
        msgs = bb.get_messages()
    except Exception as e:
        logger.warning("could not read blackboard messages: %s", e)
        return {}
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender == "weekly_meal_planner":
            data = getattr(m, "data", None) or {}
            if isinstance(data, dict) and data:
                return data
    return {}


if __name__ == "__main__":
    main()
