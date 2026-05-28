"""CLI to run the weekly_meal_planner once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planner
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planner --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_weekly_meal_planner --no-persist

Runs the strategic weekly planner: produces a plan.weekly_meals pod that
the daily_meal_proposer reads, plus (re)builds the agent's weekly shopping
list Google Doc.
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
from app.assistant.subconscious.meal_persist import apply_weekly_meal_planner_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the weekly_meal_planner once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't mint pods or write doc.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"WEEKLY MEAL PLANNER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_weekly_meal_planner_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking weekly_meal_planner...")
    agent = DI.agent_factory.create_agent("weekly_meal_planner")
    if agent is None:
        print("ERROR: failed to create weekly_meal_planner agent.")
        sys.exit(1)
    scope = ScopeAdapter.for_internal_invocation(

        scope_id="subconscious::weekly_meal_planner",

        actor_id="run_weekly_meal_planner",

    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("weekly_meal_planner agent raised")
        sys.exit(2)
    output: Dict[str, Any] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        output = result.data
    elif isinstance(result, dict):
        output = result
    else:
        print(f"WARNING: unexpected result type: {type(result).__name__}")
        sys.exit(3)

    # 3. Print summary
    print("\n[3/4] Output:")
    plan = output.get("weekly_plan") or {}
    print(f"      week_start_date: {plan.get('week_start_date')}")
    print(f"      theme: {plan.get('week_theme', '')[:200]}")
    slots = plan.get("slots") or []
    print(f"      slots ({len(slots)}):")
    from collections import Counter
    types = Counter(s.get("slot_type") for s in slots)
    for t, n in types.most_common():
        print(f"        {t}: {n}")
    weekly_list = output.get("weekly_shopping_list") or {}
    print(f"      weekly_shopping_list: action={weekly_list.get('action')} week_start={weekly_list.get('week_start_date')}")
    body = (weekly_list.get("body_markdown") or "").strip()
    if body:
        body_lines = body.split("\n")
        print(f"        body ({len(body)} chars), first 6 lines:")
        for line in body_lines[:6]:
            print(f"          {line}")
        if len(body_lines) > 6:
            print(f"          ... ({len(body_lines) - 6} more lines)")
    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:400]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pods or writing doc.")
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


if __name__ == "__main__":
    main()
