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
    args = parser.parse_args()
    scope_ctx = agents.scope()
    for day in args.days:
        ins, provenance = day_items.insights(day)
        print("=" * 90)
        print(f"{day}: {len(ins)} insight(s), {len(provenance)} source entries")
        for i in ins:
            print(f"  INSIGHT {i['n']} [{i['scope']}]: {i['insight']}  (sources {[s['n'] for s in i['sources']]})")
        for t in range(1, args.trials + 1):
            beliefs = agents.extract_day(day, ins, provenance, scope_ctx)
            print(f"  --- trial {t}: {len(beliefs)} belief(s)")
            for b in beliefs:
                print(f"    [{b['kind']}] (insight {b['insight_ref']}) {b['statement']}")
                for s in b["sources"]:
                    print(f"        #{s['kind']} {s['time']}: {s['text']}")
                print(f"        why: {b['reasoning']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
