"""CLI to run weekly_meal_manager once.

Manager version of run_weekly_meal_planner. Wraps the existing single-shot
agent in a two-phase loop:
  1. weekly_meal::context_planner reasons over the seeded context and
     decides whether tool gathering is needed. On a cold-start week, it
     typically returns control immediately (no precedent to consult).
  2. weekly_meal_planner runs as the final step and produces the
     structured WeeklyMealPlan + WeeklyShoppingList.

The output shape is identical to the single-shot run, so the existing
meal_persist.apply_weekly_meal_planner_output works unchanged.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_manager
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_manager --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_manager --no-persist
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_manager --week-start 2026-05-25
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator


def _ensure_manager_runtime_registered() -> None:
    """test_setup registers manager_invoker but not mam_instance_manager
    (which the invoker needs). Patch the gap so this runner works the
    way the full-bootstrap kg_investigator path does.
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
    parser = argparse.ArgumentParser(description="Run weekly_meal_manager once.")
    parser.add_argument("--dry-run", action="store_true", help="Print seed context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run manager but don't mint pods or write doc.")
    parser.add_argument(
        "--week-start",
        type=str,
        default=None,
        help="Override week_start_date (YYYY-MM-DD). Defaults to the date already in the seed context.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"WEEKLY MEAL MANAGER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building seed context...")
    context = build_weekly_meal_planner_context()
    if args.week_start:
        context["week_start_date"] = args.week_start
        print(f"        week_start_date overridden to {args.week_start}")
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full seed context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking weekly_meal_manager...")
    _ensure_manager_runtime_registered()
    manager = DI.multi_agent_manager_factory.create_manager("weekly_meal_manager")
    if manager is None:
        print("ERROR: failed to create weekly_meal_manager.")
        sys.exit(1)
    scope = ScopeContext(
        scope_id="subconscious::weekly_meal_manager",
        owner_id="system",
        actor_id="run_weekly_meal_manager",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    # Seed the manager's shared blackboard with the context dict so BOTH
    # context_planner and weekly_meal_planner can resolve their
    # user_context_items from it. Passing agent_input on the Message reaches
    # the delegator only — it doesn't propagate across agent handoffs.
    bb = manager.blackboard
    for k, v in context.items():
        bb.update_state_value(k, v)

    msg = Message(agent_input=context, scope_context=scope)
    try:
        DI.manager_invoker.invoke(manager, msg)
    except Exception as e:
        print(f"ERROR: manager raised: {type(e).__name__}: {e}")
        logger.exception("weekly_meal_manager raised")
        sys.exit(2)

    # 3. Recover the structured output from the manager's blackboard.
    # weekly_meal_planner runs as the final step inside the manager; its
    # AgentForm output should be on the blackboard under the same key the
    # single-shot path uses.
    output = _recover_manager_output(manager)

    print("\n[3/4] Output:")
    plan = output.get("weekly_plan") or {}
    print(f"      week_start_date: {plan.get('week_start_date')}")
    print(f"      theme: {plan.get('week_theme', '')[:200]}")
    anchors = plan.get("anchor_meals") or []
    print(f"      anchor_meals ({len(anchors)}): {', '.join(anchors)}")
    slots = plan.get("slots") or []
    print(f"      slots ({len(slots)}):")
    from collections import Counter
    types = Counter(s.get("slot_type") for s in slots)
    for t, n in types.most_common():
        print(f"        {t}: {n}")
    print(f"      addressed_concern_ids: {output.get('addressed_concern_ids') or []}")
    weekly_list = output.get("weekly_shopping_list") or {}
    print(f"      weekly_shopping_list: action={weekly_list.get('action')} week_start={weekly_list.get('week_start_date')}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pods or writing doc.")
        return

    if not output:
        print("\n[4/4] No output recovered from manager; skipping persist.")
        return

    print("\n[4/4] Persisting plan pod + updating weekly shopping doc...")
    summary = apply_weekly_meal_planner_output(output)
    for k, v in summary.items():
        if isinstance(v, list):
            print(f"      {k}: ({len(v)} items) {v[:5]}{'...' if len(v) > 5 else ''}")
        else:
            print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


def _recover_manager_output(manager) -> Dict[str, Any]:
    """Pull weekly_meal_planner's structured output from the manager blackboard.

    Pattern matches kg_investigator._extract_report_from_audit: each agent's
    output lands as a Message on the blackboard with sender = agent name and
    .data carrying the parsed AgentForm. We walk messages, find the one from
    weekly_meal_planner, return its .data dict.
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
