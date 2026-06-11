"""CLI to run the weekly meal planning chain end-to-end.

Thin wrapper over ``weekly_meal_planning_runner.run_weekly_meal_planning_chain`` — the
SAME path the Sunday cron (weekly_meal_planner_run) and the /meals "Generate" button use.
Stages: meal_context_distiller -> weekly_meal_planning_manager (delegator -> planner ->
[meal_research] -> weekly_meal_planner) -> meal_persist.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning --no-persist
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planning --week-start 2026-05-25
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401  (bootstraps DI for standalone use)

from app.assistant.subconscious.weekly_meal_planning_runner import (  # noqa: E402
    run_weekly_meal_planning_chain,
)
from app.assistant.utils.logging_config import get_logger  # noqa: E402

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the weekly meal planning chain.")
    parser.add_argument("--no-persist", action="store_true",
                        help="Run distiller + manager but don't mint pods or write the doc.")
    parser.add_argument("--week-start", type=str, default=None,
                        help="Override week_start_date (YYYY-MM-DD Monday).")
    args = parser.parse_args()

    print("=" * 70)
    print("WEEKLY MEAL PLANNING (distiller -> planning manager -> persist)")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    summary = run_weekly_meal_planning_chain(
        week_start=args.week_start, persist=not args.no_persist
    )

    print(f"\nweek_start_date  : {summary.get('week_start_date')}")
    print(f"meal_context_days: {summary.get('meal_context_days')}")

    if args.no_persist:
        out = summary.get("output") or {}
        plan = out.get("weekly_plan") or {}
        slots = plan.get("slots") or []
        print(f"theme: {plan.get('week_theme', '')[:200]}")
        print(f"slots ({len(slots)}): {dict(Counter(s.get('slot_type') for s in slots))}")
        wl = out.get("weekly_shopping_list") or {}
        print(f"weekly_shopping_list: action={wl.get('action')}")
        print("\n(--no-persist set; nothing minted)")
    else:
        for k, v in summary.items():
            if k in ("week_start_date", "meal_context_days"):
                continue
            if isinstance(v, list):
                print(f"{k}: ({len(v)} items) {v[:5]}{'...' if len(v) > 5 else ''}")
            else:
                print(f"{k}: {v}")

    print("\n" + "=" * 70 + "\nDONE\n" + "=" * 70)


if __name__ == "__main__":
    main()
