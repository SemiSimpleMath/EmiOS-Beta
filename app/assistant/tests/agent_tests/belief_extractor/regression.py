"""Regression set for belief_engine::belief_extractor. READ-ONLY. Run after EVERY extractor prompt change.

Each case is a real day and the lesson it pins, checked with plain text patterns over the atoms the
pipeline would create (via belief_engine.intake — the same code path as the replay). Patterns are a
guard, not a judge of quality: every atom is printed so a person can read the day too.

Run from repo root:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_extractor.regression [--trials N]
"""
from __future__ import annotations

import argparse
import re
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from belief_engine.intake import agents, day_items

# One day's atoms state what happened; habits come from the weekly step, across days.
HABIT = r"\b(usually|often|typically|tends to|routinely|generally|habitually)\b"

# day -> (lesson, patterns that must appear in some atom, patterns that must appear in none)
CASES = {
    "2026-09-25": ("separate replies stay separate decisions; no broad 'personal control' rule",
                   [r"MyBanfield"], [r"purchas", r"submit\w* forms?", r"personal control"]),
    "2026-09-24": ("a detail is understood on its own (which link, whose)",
                   [r"annual physical", r"Marika.*(invit|link)"], [r"\bthe link\b", r"\bthe private key\b"]),
    "2026-08-31": ("one evening yields nothing lasting, or at most a dated fact",
                   [], [r"before (10|22)", r"boundary", r"nudges?", r"reminders?", HABIT]),
    "2026-09-13": ("no rule the user never stated",
                   [r"poset"], [r"minimi[sz]", r"API-heavy", r"unnecessary"]),
    "2026-09-22": ("a stated allergy is kept",
                   [r"allergic to raw carrots"], []),
    "2026-02-12": ("a dated event (who took the dogs out) stays a dated event, never a habit — it is the "
                   "evidence that later reshapes a belief like who usually walks the dogs",
                   [], [HABIT]),
    "2026-09-15": ("a dated fact the user stated is kept",
                   [r"flea"], []),
    "2026-03-08": ("a day of reminder traffic is not turned into a log of declines and completions",
                   [], [r"\b(declined|deferred|accepted|completed)\b.*\b(suggestion|prompt|reminder)",
                        r"\bat \d{1,2}:\d\d\b"]),
}
# Payload retention (a date or an address the user gave reaching a statement: 2026-08-18 the spouse's
# birthday, 2026-06-13 Pieter's address) was measured 2026-09-27 and is NOT pinned: the prompt
# rule that fixed it (1/4 -> 4/4) also brought back dated one-off log entries (2026-03-08,
# 2026-08-31), so it was withdrawn — design doc §10a defect 2. A detail no insight noticed
# (2026-06-07: addresses in a ticket reply) never reaches the extractor at all.
# Instructions to the assistant with no owner. A wish the user stated ("said no subagents should
# handle it") is attributed and allowed, so only an unattributed sentence opening is checked.
EVERY_DAY_ABSENT = [r"^(the assistant|emi|agents?)\b.*\b(should|must)\b"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--days", nargs="*", help="run only these cases")
    args = parser.parse_args()
    scope_ctx = agents.scope()

    failures = 0
    for day, (lesson, present, absent) in CASES.items():
        if args.days and day not in args.days:
            continue
        ins = day_items.insights(day)
        items, provenance = day_items.timeline_items(day)
        for trial in range(1, args.trials + 1):
            try:
                atoms = agents.extract_day(day, ins, items, provenance, scope_ctx)
            except Exception as exc:
                print(f"FAIL {day} t{trial}: extraction raised {type(exc).__name__}: {exc}")
                failures += 1
                continue
            text = [a["statement"] for a in atoms]
            problems = [f"missing /{p}/" for p in present if not any(re.search(p, t, re.I) for t in text)]
            problems += [f"forbidden /{p}/ in: {t}" for p in absent + EVERY_DAY_ABSENT
                         for t in text if re.search(p, t, re.I)]
            status = "PASS" if not problems else "FAIL"
            failures += bool(problems)
            print(f"{status} {day} t{trial} — {lesson} ({len(atoms)} atoms)")
            for p in problems:
                print(f"       {p}")
            for t in text:
                print(f"       · {t}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
