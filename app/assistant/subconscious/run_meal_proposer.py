"""CLI entry point to run the meal_proposer once and inspect the output.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_proposer
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_proposer --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_proposer --no-persist

Same shape as run_noticer.py — builds context, invokes the agent, prints
a human summary, optionally persists proposals as pods.
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
from app.assistant.subconscious.meal_context_builder import build_meal_proposer_context
from app.assistant.subconscious.meal_persist import apply_meal_proposer_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the meal_proposer once.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print assembled context and exit. No LLM call, no writes.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Run the agent but don't mint intention pods.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"MEAL PROPOSER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # 1. Build context
    print("\n[1/4] Building context...")
    context = build_meal_proposer_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    # 2. Invoke agent
    print("\n[2/4] Invoking meal_proposer agent...")
    agent_name = "meal_proposer"
    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        print(f"ERROR: failed to create agent {agent_name!r}.")
        sys.exit(1)

    scope = ScopeContext(
        scope_id="subconscious::meal_proposer",
        owner_id="system",
        actor_id="run_meal_proposer",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    msg = Message(agent_input=context, scope_context=scope)

    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("meal_proposer agent raised")
        sys.exit(2)

    output: Dict[str, Any] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        output = result.data
    elif isinstance(result, dict):
        output = result
    else:
        print(f"WARNING: unexpected result type: {type(result).__name__}")
        print(repr(result)[:500])
        sys.exit(3)

    # 3. Print human-readable summary
    print("\n[3/4] Meal proposer output:")
    proposals = output.get("proposals") or []
    print(f"      proposals: {len(proposals)}")
    for p in proposals:
        print(
            f"        • [{p.get('confidence')}/{p.get('novelty')}] "
            f"{p.get('meal_window')} {p.get('date')} → "
            f"{p.get('dish')}  ({p.get('source')})"
        )
        if p.get("recipe_ref"):
            print(f"          recipe: {p['recipe_ref']}")
        actors = p.get("actors") or []
        print(f"          actors: {', '.join(actors)}")
        needs = p.get("needs_shopping") or []
        if needs:
            print(f"          needs_shopping: {', '.join(needs)}")
        addresses = p.get("addresses_concern_ids") or []
        if addresses:
            print(f"          addresses concerns: {', '.join(addresses)}")
        reasoning = (p.get("reasoning") or "").strip()
        if reasoning:
            print(f"          reasoning: {reasoning[:240]}")
        if p.get("novelty_rationale"):
            print(f"          novelty_rationale: {p['novelty_rationale'][:240]}")

    shopping = output.get("shopping_run")
    if shopping:
        items = shopping.get("items") or []
        print(f"      shopping_run: {shopping.get('suggested_date')} — {len(items)} items")
        if items:
            print(f"        items: {', '.join(items)}")

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

    # 4. Persist
    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting intention pods.")
        return

    print("\n[4/4] Minting intention pods...")
    summary = apply_meal_proposer_output(output)
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
