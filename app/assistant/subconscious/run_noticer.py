"""CLI entry point to run the noticer once and inspect the issues it raises.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_noticer              # print the issues only
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_noticer --context    # print the context, no model call
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_noticer --raise      # also put them in the brain's inbox

The noticer raises issues for the brain's inbox and writes nothing else (2026-10-09).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

# Bootstrap DI before any project imports that touch services.
import app.assistant.tests.test_setup  # noqa: F401  bootstrap

from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
from app.assistant.subconscious.context_builder import build_noticer_context


def main():
    parser = argparse.ArgumentParser(description="Run the subconscious noticer once.")
    parser.add_argument("--context", action="store_true", help="Print the assembled context; no model call.")
    parser.add_argument("--raise", dest="raise_issues", action="store_true",
                        help="Put the issues in the brain's inbox (default: print only).")
    args = parser.parse_args()

    context = build_noticer_context()
    if args.context:
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return

    output = _run_subconscious_agent(handler_label="run_noticer", agent_name="subconscious::noticer", context=context,
                                     scope_id="subconscious::noticer", actor_id="run_noticer")
    print(f"summary: {output.get('summary')}")
    for issue in output.get("issues") or []:
        print(f"\n- [{issue['kind']}] {issue['title']} (about {issue['subject']})\n  {issue['what']}\n"
              f"  why now: {issue['why_now']}")

    if args.raise_issues:
        from app.assistant.subconscious import brain_inbox
        raised = brain_inbox.add_noticer_issues(output.get("issues") or [], run_at=datetime.now(timezone.utc))
        print(f"\nraised {raised} issue(s) in the brain's inbox")


if __name__ == "__main__":
    main()
