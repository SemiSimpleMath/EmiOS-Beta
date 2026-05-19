"""CLI entry point to run the noticer once and inspect the output.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_noticer
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_noticer --trigger-mode weekly_plan

The noticer is invoked via invoke_agent, just like a compiled task would do.
After the call returns, the structured AgentForm output is parsed, persisted
to resources/subconscious/, and a human-readable summary is printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Bootstrap DI before any project imports that touch services.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app.assistant.tests.test_setup  # noqa: F401  bootstrap

from app.assistant.subconscious.context_builder import build_noticer_context
from app.assistant.subconscious.persist import apply_noticer_output
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the subconscious noticer once.")
    parser.add_argument(
        "--trigger-mode",
        default="daily",
        choices=["daily", "weekly_plan", "event_reactive", "special_day", "nudge"],
        help="Trigger mode (default: daily).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the assembled context but do not call the LLM.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Run the noticer but don't write to concerns_register.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"NOTICER RUN — trigger_mode={args.trigger_mode}")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # 1. Build context
    print("\n[1/4] Building context...")
    context = build_noticer_context(trigger_mode=args.trigger_mode)

    print(f"      Assembled {len(context)} context keys.")
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:30s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    # 2. Invoke noticer
    print("\n[2/4] Invoking subconscious::noticer agent...")
    agent_name = "subconscious::noticer"
    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        print(f"ERROR: failed to create agent {agent_name!r}.")
        sys.exit(1)

    scope = ScopeContext(
        scope_id=f"subconscious::{agent_name}",
        owner_id="system",
        actor_id="run_noticer",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    msg = Message(agent_input=context, scope_context=scope)

    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("noticer agent raised")
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
    print("\n[3/4] Noticer output:")
    print(f"      summary: {output.get('summary', '(none)')[:400]}")
    print(f"      skipped_pass_b: {output.get('skipped_pass_b')}")
    if output.get("skipped_pass_b_reason"):
        print(f"      skipped_pass_b_reason: {output.get('skipped_pass_b_reason')}")
    print(f"      new_concerns: {len(output.get('new_concerns') or [])}")
    for c in output.get("new_concerns") or []:
        print(f"        • [{c.get('severity')}/{c.get('horizon')}] {c.get('title')}  → {','.join(c.get('addressable_by') or [])}")
        print(f"          subject={c.get('subject')}  kind={c.get('kind')}")
        print(f"          notes: {(c.get('notes') or '')[:200]}")
        ev = c.get("evidence") or []
        print(f"          evidence: {len(ev)} item(s)")
        for e in ev[:3]:
            print(f"            - {e.get('kind')}: {e.get('ref')}  {(e.get('snippet') or '')[:80]}")

    print(f"      reinforced_concerns: {len(output.get('reinforced_concerns') or [])}")
    for r in output.get("reinforced_concerns") or []:
        print(f"        • {r.get('concern_id')}: +{len(r.get('new_evidence') or [])} evidence, severity_change={r.get('severity_change')}")

    print(f"      resolved_concerns: {len(output.get('resolved_concerns') or [])}")
    for r in output.get("resolved_concerns") or []:
        print(f"        • {r.get('concern_id')}: {r.get('reason')[:120]}")

    print(f"      escalated_concerns: {len(output.get('escalated_concerns') or [])}")
    for e in output.get("escalated_concerns") or []:
        print(f"        • {e.get('concern_id')} → {e.get('target')} ({e.get('urgency')}): {e.get('reason')[:120]}")

    print(f"      belief_updates: {len(output.get('belief_updates') or [])}")
    for b in output.get("belief_updates") or []:
        print(f"        • [{b.get('polarity')}/{b.get('confidence')}] {b.get('belief_kind')}: {b.get('claim')[:120]}")

    print(f"      pending_questions: {len(output.get('pending_questions') or [])}")
    for q in output.get("pending_questions") or []:
        print(f"        ? {q.get('text')}")
        print(f"          why: {q.get('why_asking')}")
        print(f"          default if unanswered: {q.get('if_unanswered')}")

    # 4. Persist
    if args.no_persist:
        print("\n[4/4] --no-persist set; not writing to concerns_register.")
    else:
        print("\n[4/4] Persisting to concerns_register...")
        summary = apply_noticer_output(output)
        for k, v in summary.items():
            print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
