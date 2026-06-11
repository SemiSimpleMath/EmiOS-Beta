"""CLI to run the daily_meal_proposer once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_daily_meal_proposer
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_daily_meal_proposer --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_daily_meal_proposer --no-persist

Tactical 24-48h meal proposer. Reads the latest plan.weekly_meals pod for
context; turns slots into concrete dishes. Mints intention.meal pods.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.meal_context_builder import build_daily_meal_proposer_context
from app.assistant.subconscious.meal_persist import apply_daily_meal_proposer_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the daily_meal_proposer once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't mint pods.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"DAILY MEAL PROPOSER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_daily_meal_proposer_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking daily_meal_proposer...")
    agent = DI.agent_factory.create_agent("daily_meal_proposer")
    if agent is None:
        print("ERROR: failed to create daily_meal_proposer agent.")
        sys.exit(1)
    scope = load_scope_for_source(
        kind="subsystem",
        source_id="subconscious",
        actor_id="run_daily_meal_proposer",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::daily_meal_proposer", "surface": "internal"},
    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("daily_meal_proposer agent raised")
        sys.exit(2)

    output: Dict[str, Any] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        output = result.data
    elif isinstance(result, dict):
        output = result
    else:
        print(f"WARNING: unexpected result type: {type(result).__name__}")
        sys.exit(3)

    print("\n[3/4] Output:")
    proposals = output.get("proposals") or []
    print(f"      proposals: {len(proposals)}")
    for p in proposals:
        print(
            f"        • [{p.get('confidence')}/{p.get('novelty')}] "
            f"{p.get('meal_window')} {p.get('date')} → {p.get('dish')}  ({p.get('source')})"
        )
        actors = p.get("actors") or []
        print(f"          actors: {', '.join(actors)}")
        slot = p.get("fills_weekly_plan_slot")
        if slot:
            print(f"          fills slot: {slot}")
        needs = p.get("needs_shopping") or []
        if needs:
            print(f"          needs_shopping: {', '.join(needs)}")
        reasoning = (p.get("reasoning") or "").strip()
        if reasoning:
            print(f"          reasoning: {reasoning[:220]}")

    shopping = output.get("shopping_run")
    if shopping:
        items = shopping.get("items") or []
        print(f"      shopping_run (small/ad-hoc): {len(items)} items: {', '.join(items)}")

    skipped = output.get("skipped_meals") or []
    if skipped:
        print(f"      skipped_meals: {len(skipped)}")
        for s in skipped:
            print(f"        • {s}")

    advisory = output.get("fast_food_advisory")
    if advisory:
        print(f"      fast_food_advisory: {advisory[:240]}")

    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:400]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pods.")
        return

    print("\n[4/4] Minting intention pods...")
    summary = apply_daily_meal_proposer_output(output)
    for k, v in summary.items():
        if isinstance(v, list):
            print(f"      {k}: ({len(v)} ids)")
        else:
            print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
