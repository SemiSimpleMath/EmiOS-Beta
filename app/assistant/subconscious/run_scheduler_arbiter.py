"""CLI to run the scheduler_arbiter once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_scheduler_arbiter
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_scheduler_arbiter --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_scheduler_arbiter --no-persist

Reads every candidate intention.* pod for the next 14 days from
meal / wellness / romantic, resolves conflicts using priority rules +
LLM judgment, and emits ONE authoritative plan.weekly_schedule pod
that proposers honor on their next run. Surfaces unresolvable
conflicts as dayflow tickets.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.scheduler_arbiter_context_builder import (
    build_scheduler_arbiter_context,
)
from app.assistant.subconscious.scheduler_arbiter_persist import (
    apply_scheduler_arbiter_output,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the scheduler_arbiter once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't mint pods or tickets.")
    args = parser.parse_args()

    print("=" * 70)
    print("SCHEDULER ARBITER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_scheduler_arbiter_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:28s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking scheduler_arbiter...")
    agent = DI.agent_factory.create_agent("scheduler_arbiter")
    if agent is None:
        print("ERROR: failed to create scheduler_arbiter agent.")
        sys.exit(1)
    scope = load_scope_for_source(
        kind="subsystem",
        source_id="subconscious",
        actor_id="run_scheduler_arbiter",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::scheduler_arbiter", "surface": "internal"},
    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("scheduler_arbiter agent raised")
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
    print(f"      week_start_date: {output.get('week_start_date')}")
    schedule = output.get("weekly_schedule") or []
    anchored = sum(1 for s in schedule if s.get("is_anchor"))
    print(f"      weekly_schedule: {len(schedule)} items ({anchored} anchored)")
    from collections import defaultdict
    by_date = defaultdict(list)
    for s in schedule:
        by_date[s.get("date") or "?"].append(s)
    for d in sorted(by_date.keys()):
        print(f"        {d}:")
        for s in by_date[d]:
            mark = " 🔒" if s.get("is_anchor") else "   "
            print(f"        {mark}[{s.get('domain', '?')}] {s.get('summary', '')[:120]}")

    resolved = output.get("conflicts_resolved") or []
    if resolved:
        print(f"      conflicts_resolved: {len(resolved)}")
        for c in resolved:
            print(f"        • {c.get('conflict_summary', '')[:120]}")
            print(f"          chose: {c.get('chosen_pod_id', '')[:40]}")
            if c.get("displaced_pod_ids"):
                print(f"          displaced: {', '.join(c['displaced_pod_ids'])[:80]}")
            if c.get("priority_rule_applied"):
                print(f"          rule: {c.get('priority_rule_applied', '')[:100]}")
            print(f"          reasoning: {c.get('reasoning', '')[:200]}")

    user_conflicts = output.get("conflicts_for_user") or []
    if user_conflicts:
        print(f"      conflicts_for_user: {len(user_conflicts)}")
        for c in user_conflicts:
            print(f"        • {c.get('conflict_summary', '')[:120]}")
            for opt in (c.get("options") or [])[:4]:
                print(f"            - {opt[:100]}")

    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:500]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not minting pod or tickets.")
        return

    print("\n[4/4] Minting plan.weekly_schedule pod + surfacing tickets...")
    summary = apply_scheduler_arbiter_output(output)
    for k, v in summary.items():
        if isinstance(v, list):
            print(f"      {k}: ({len(v)} items)")
        else:
            print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
