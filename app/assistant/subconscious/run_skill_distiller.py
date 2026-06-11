"""CLI to run the skill_distiller once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_skill_distiller
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_skill_distiller --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_skill_distiller --no-persist

Weekly agent. Reviews the past 7 days of intention pods, arbiter decisions,
and chat outcome signals; proposes additions to the household's learned-
skills file for user review. NEVER auto-applies — user copies accepted
lines into canonical rule files manually.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.skill_distiller_context_builder import (
    build_skill_distiller_context,
)
from app.assistant.subconscious.skill_distiller_persist import (
    apply_skill_distiller_output,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the skill_distiller once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't append to learned_skills_proposed.")
    args = parser.parse_args()

    print("=" * 70)
    print("SKILL DISTILLER RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_skill_distiller_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:38s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    print("\n[2/4] Invoking skill_distiller...")
    agent = DI.agent_factory.create_agent("skill_distiller")
    if agent is None:
        print("ERROR: failed to create skill_distiller agent.")
        sys.exit(1)
    scope = load_scope_for_source(
        kind="subsystem",
        source_id="subconscious",
        actor_id="run_skill_distiller",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::skill_distiller", "surface": "internal"},
    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("skill_distiller agent raised")
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
    print(f"      week_window_label: {output.get('week_window_label')}")
    proposals = output.get("proposals") or []
    print(f"      proposals: {len(proposals)}")
    for p in proposals:
        kind = p.get("proposal_kind", "?")
        target = p.get("target_file", "?")
        section = p.get("target_section") or "(no section)"
        novelty = p.get("novelty", "?")
        confidence = p.get("confidence", "?")
        rule = (p.get("rule_text") or "").strip()
        why = (p.get("why_now") or "").strip()
        ev = p.get("evidence") or []
        print(f"        • [{kind}] target={target}/{section}")
        print(f"          {novelty}/{confidence}")
        print(f"          rule: {rule[:300]}")
        if why:
            print(f"          why_now: {why[:240]}")
        if ev:
            print(f"          evidence: {len(ev)} items")
            for e in ev[:3]:
                print(f"            - {e.get('source_kind', '?')} {e.get('ref', '?')[:60]}: {(e.get('snippet') or '')[:120]}")

    skipped = output.get("skipped_reasons") or []
    if skipped:
        print(f"      skipped_reasons: {len(skipped)}")
        for s in skipped:
            print(f"        - {s[:200]}")

    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:500]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not appending to learned_skills_proposed.")
        return

    print("\n[4/4] Appending to learned_skills_proposed.md...")
    summary = apply_skill_distiller_output(output)
    for k, v in summary.items():
        print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
