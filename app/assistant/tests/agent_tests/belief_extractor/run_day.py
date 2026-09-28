"""Run belief_engine::belief_extractor on given days through the real intake path. READ-ONLY.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_extractor.run_day 2026-03-08 [...] [--trials N]
"""
from __future__ import annotations

import argparse
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from belief_engine.intake import agents, day_items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("days", nargs="+")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--chronic-only", action="store_true", help="pass only insights the writer marked chronic")
    parser.add_argument("--with-sentence", action="store_true", help="pass each insight's sentence, not only its evidence")
    args = parser.parse_args()
    scope_ctx = agents.scope()
    for day in args.days:
        ins = day_items.insights(day, chronic_only=args.chronic_only, with_sentence=args.with_sentence)
        items, provenance = day_items.timeline_items(day)
        print("=" * 90)
        print(f"{day}: {len(ins)} insight(s), {len(items)} items")
        for i in ins:
            print(f"  INSIGHT {i['n']} [{i['scope']}]: {i.get('insight') or i['evidence_noted']}")
        for t in range(1, args.trials + 1):
            beliefs = agents.extract_day(day, ins, items, provenance, scope_ctx)
            print(f"  --- trial {t}: {len(beliefs)} belief(s)")
            for b in beliefs:
                print(f"    [{b['kind']}] (insight {b['insight_ref']}) {b['statement']}")
                for s in b["sources"]:
                    print(f"        #{s['kind']} {s['time']}: {s['text']}")
                print(f"        why: {b['reasoning']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
