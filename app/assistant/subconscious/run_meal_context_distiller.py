"""CLI to run the meal_context_distiller and inspect its output.

The distiller is a cheap LLM step that reorganizes the seed (beliefs +
concerns + calendar + roster + inventory) into a per-day structure the
downstream meal_planner can scan directly. This runner exists so we can
see what the distiller produces before wiring it into the planning
pipeline.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_context_distiller
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_context_distiller --horizon-days 7
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_context_distiller --week-start 2026-05-25
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

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.meal_context_builder import build_weekly_meal_planner_context
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run meal_context_distiller once and print output.")
    parser.add_argument("--horizon-days", type=int, default=7, help="Planning horizon in days (2 for daily, 7 for weekly).")
    parser.add_argument("--week-start", type=str, default=None, help="Override week_start_date (YYYY-MM-DD).")
    args = parser.parse_args()

    print("=" * 70)
    print(f"MEAL CONTEXT DISTILLER  horizon={args.horizon_days} days")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/3] Building seed context...")
    context = build_weekly_meal_planner_context()
    if args.week_start:
        context["week_start_date"] = args.week_start
        print(f"        week_start_date overridden to {args.week_start}")
    context["horizon_days"] = str(args.horizon_days)

    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        print(f"        {k:28s}  ({len(v_str):>6} chars)")

    print("\n[2/3] Invoking meal_context_distiller...")
    agent = DI.agent_factory.create_agent("meal_context_distiller")
    if agent is None:
        print("ERROR: failed to create meal_context_distiller agent.")
        sys.exit(1)
    scope = ScopeAdapter.for_internal_invocation(

        scope_id="subconscious::meal_context_distiller",

        actor_id="run_meal_context_distiller",

    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("meal_context_distiller raised")
        sys.exit(2)

    output: Dict[str, Any] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        output = result.data
    elif isinstance(result, dict):
        output = result
    else:
        print(f"WARNING: unexpected result type: {type(result).__name__}")
        sys.exit(3)

    print("\n[3/3] Distiller output:\n")
    print("=" * 70)
    print(f"SUMMARY: {output.get('horizon_summary', '')}")
    print("=" * 70)

    relevant = output.get("relevant_beliefs") or []
    print(f"\nRelevant beliefs ({len(relevant)}):")
    for i, b in enumerate(relevant, 1):
        print(f"  {i:2d}. {b}")

    hwc = output.get("horizon_wide_concerns") or ""
    if hwc.strip():
        print(f"\nHorizon-wide concerns:")
        print(f"  {hwc}")

    inv = output.get("inventory_pressures") or ""
    if inv.strip():
        print(f"\nInventory pressures:")
        print(f"  {inv}")

    days = output.get("days") or []
    print(f"\nDays ({len(days)}):")
    for d in days:
        date = d.get("date", "?")
        dow = d.get("day_of_week", "?")
        print(f"\n  --- {date} ({dow}) ---")
        a = d.get("audience_notes", "")
        e = d.get("events_or_constraints", "")
        n = d.get("notes", "")
        if a:
            print(f"  audience: {a}")
        if e:
            print(f"  events:   {e}")
        if n:
            print(f"  notes:    {n}")
        if not (a or e or n):
            print(f"  (default audience, no notable events)")

    add = output.get("additional_notes") or ""
    if add.strip():
        print(f"\nAdditional notes:")
        print(f"  {add}")

    print("\n" + "=" * 70)
    print("RAW JSON:")
    print("=" * 70)
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
