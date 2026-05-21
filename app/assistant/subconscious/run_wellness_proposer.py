"""CLI to run the wellness_proposer once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_wellness_proposer
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_wellness_proposer --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_wellness_proposer --no-persist

Daily wellness brain. Reads activity + sleep signals + concerns;
proposes workouts, sleep routines, recovery, hydration, mobility breaks,
meditation for the next 24-48h. Mints intention.wellness pods.
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
from app.assistant.subconscious.wellness_context_builder import (
    build_wellness_proposer_context,
)
from app.assistant.subconscious.wellness_persist import apply_wellness_proposer_output
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the wellness_proposer once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't mint pods.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"WELLNESS PROPOSER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_wellness_proposer_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking wellness_proposer...")
    agent = DI.agent_factory.create_agent("wellness_proposer")
    if agent is None:
        print("ERROR: failed to create wellness_proposer agent.")
        sys.exit(1)
    scope = ScopeContext(
        scope_id="subconscious::wellness_proposer",
        owner_id="system",
        actor_id="run_wellness_proposer",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("wellness_proposer agent raised")
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
            f"{p.get('kind')} {p.get('date')} → {p.get('summary')}"
        )
        actors = p.get("actors") or []
        print(f"          actors: {', '.join(actors)}")
        wt = p.get("workout_type")
        if wt:
            intensity = p.get("intensity") or "?"
            dur = p.get("duration_minutes") or "?"
            print(f"          workout: {wt}, intensity={intensity}, {dur}min")
        equipment = p.get("equipment_used") or []
        if equipment:
            print(f"          equipment: {', '.join(equipment)}")
        reasoning = (p.get("reasoning") or "").strip()
        if reasoning:
            print(f"          reasoning: {reasoning[:220]}")

    skipped = output.get("skipped_today") or []
    if skipped:
        print(f"      skipped_today: {len(skipped)}")
        for s in skipped:
            print(f"        • {s}")

    advisory = output.get("rest_day_advisory")
    if advisory:
        print(f"      rest_day_advisory: {advisory[:240]}")

    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:400]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pods.")
        return

    print("\n[4/4] Minting intention pods...")
    summary = apply_wellness_proposer_output(output)
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
