"""CLI to run the feedback_extractor once.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_feedback_extractor
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_feedback_extractor --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_feedback_extractor --no-persist

Reads queued feedback.comment pods (where metadata.processed_at_utc is
None), interprets each comment in light of its target pod, and emits
belief_updates into the household belief store via
BeliefStore.upsert_belief. Each processed comment gets marked
processed_at_utc + extracted_belief_ids so the next run skips it.

Designed to run hourly (or on-demand). Closes the input→storage piece
of the feedback loop: comment → belief → (next-day proposer reads
belief snapshot → behavior shifts).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.feedback_extractor_context_builder import (
    build_feedback_extractor_context,
)
from app.assistant.subconscious.feedback_extractor_persist import (
    apply_feedback_extractor_output,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the feedback_extractor once.")
    parser.add_argument("--dry-run", action="store_true", help="Print context and exit.")
    parser.add_argument("--no-persist", action="store_true", help="Run agent but don't upsert beliefs or mark comments.")
    args = parser.parse_args()

    print("=" * 70)
    print("FEEDBACK EXTRACTOR RUN")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    print("\n[1/4] Building context...")
    context = build_feedback_extractor_context()
    for k, v in context.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:32s}  ({len(v_str):>6} chars)  {preview}")

    if args.dry_run:
        print("\n--- DRY RUN: full context ---")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    # Quick early-exit if there's nothing to do
    if "no unprocessed feedback.comment pods" in context.get("unprocessed_comments_block", ""):
        print("\n[2/4] No comments to process — exiting clean.")
        return

    print("\n[2/4] Invoking feedback_extractor...")
    agent = DI.agent_factory.create_agent("feedback_extractor")
    if agent is None:
        print("ERROR: failed to create feedback_extractor agent.")
        sys.exit(1)
    scope = load_scope_for_source(
        kind="subsystem",
        source_id="subconscious",
        actor_id="run_feedback_extractor",
        identity_overrides={"owner_id": "system", "scope_id": "subconscious::feedback_extractor", "surface": "internal"},
    )
    msg = Message(agent_input=context, scope_context=scope)
    try:
        result = agent.action_handler(msg)
    except Exception as e:
        print(f"ERROR: agent raised: {type(e).__name__}: {e}")
        logger.exception("feedback_extractor agent raised")
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
    extractions = output.get("extractions") or []
    print(f"      extractions: {len(extractions)}")
    for ext in extractions:
        print(
            f"        • [{ext.get('signal_type')}/{ext.get('confidence')}/{ext.get('scope')}] "
            f"{ext.get('domain')}: {ext.get('belief_key')}"
        )
        print(f"          statement: {(ext.get('statement') or '')[:240]}")
        print(f"          from comment: {ext.get('source_comment_pod_id')}")
        print(f"          target: {ext.get('target_pod_id')}")
        reason = (ext.get("reasoning") or "").strip()
        if reason:
            print(f"          reasoning: {reason[:200]}")

    skipped = output.get("skipped") or []
    if skipped:
        print(f"      skipped: {len(skipped)}")
        for s in skipped:
            print(f"        • {s.get('comment_pod_id')}: {s.get('reason', '')[:200]}")

    thinking = (output.get("free_form_thinking") or "").strip()
    if thinking:
        print(f"      free_form_thinking: {thinking[:400]}")

    if args.no_persist:
        print("\n[4/4] --no-persist set; not upserting beliefs or marking comments.")
        return

    print("\n[4/4] Upserting beliefs + marking comments processed...")
    summary = apply_feedback_extractor_output(output)
    for k, v in summary.items():
        if isinstance(v, list):
            print(f"      {k}: ({len(v)} items)")
            for item in v[:5]:
                print(f"        - {item}")
        else:
            print(f"      {k}: {v}")

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
