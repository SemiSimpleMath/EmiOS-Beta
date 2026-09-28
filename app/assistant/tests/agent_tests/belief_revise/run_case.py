"""Re-run belief_engine::belief_revise on recorded revisions, N times each. READ-ONLY.

Reconstructs the belief as it stood before the revision (from a replay store's revisions table
and the evidence dated before that day) and the new belief that triggered it (from that replay's
cached extraction), then calls the revise agent. Nothing is written.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_revise.run_case B152 --trials 3
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_revise.run_case --all
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_revise.run_case --all --store scratch/belief_replay_40d_v6

Besides printing every restatement it counts two shapes a belief should rarely take: a DIARY
(two or more dated clauses — a history of who said what when, which is the evidence's job) and a
HEDGE ("varies", "has varied", "does not yet establish" — the agent declining to decide).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.utils.path_utils import get_repo_root
from belief_engine.intake import agents

DATE = re.compile(r"\b(?:on|before|after|until|as of|since)\s+(?:\d{4}-\d\d-\d\d|(?:January|February|March|April|May|June|July|"
                  r"August|September|October|November|December)\s+\d{1,2}(?:,\s*\d{4})?)", re.I)
HEDGE = re.compile(r"\b(varies|has varied|have varied|does not yet establish|not yet establish)\b", re.I)


def shape(statement: str) -> str:
    tags = []
    if len(DATE.findall(statement)) >= 2:
        tags.append("DIARY")
    if HEDGE.search(statement):
        tags.append("HEDGE")
    return " ".join(tags)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("belief_ids", nargs="*")
    parser.add_argument("--all", action="store_true", help="every revision the store recorded")
    parser.add_argument("--store", default="scratch/belief_replay", help="replay output dir")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    root = get_repo_root()
    store = root / args.store
    c = sqlite3.connect(f"{(store / 'belief_replay.db').resolve().as_uri()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    revs = [dict(r) for r in c.execute("SELECT * FROM revisions ORDER BY id")]
    if not args.all:
        revs = [r for r in revs if r["belief_id"] in args.belief_ids]
    if not revs:
        raise SystemExit("no matching revision recorded")
    scope_ctx = agents.scope()
    n = diary = hedge = unchanged = 0
    seen: dict[tuple[str, str], int] = {}
    for rev in revs:
        bid, day = rev["belief_id"], rev["day"]
        before = [dict(r) for r in c.execute(
            "SELECT day, time, kind, relation, text, in_reply_to FROM evidence WHERE belief_id=? AND day<? ORDER BY id",
            (bid, day))]
        held = {"id": bid, "statement": rev["old_statement"], "kind": rev["old_kind"], "sources": before}
        # The n-th revision of a belief on a day answers the n-th contradiction recorded against it that day.
        nth = seen.get((bid, day), 0)
        seen[(bid, day)] = nth + 1
        decision = c.execute("SELECT statement FROM decisions WHERE day=? AND verdict='contradicts' AND result_belief=? "
                             "ORDER BY id LIMIT 1 OFFSET ?", (day, bid, nth)).fetchone()
        extract = json.loads((store / "extract" / f"{day}.json").read_text(encoding="utf-8"))
        atom = next(a for a in extract["atoms"] if a["statement"] == decision["statement"])
        print("=" * 100)
        print(f"{day} {bid}\nHELD ({rev['old_kind']}): {rev['old_statement']}")
        print(f"NEW  ({atom['kind']}): {atom['statement']}")
        for s in atom["sources"]:
            print(f"      {s['kind']} {s['time']}: {s['text']}")
        print(f"RECORDED: {shape(rev['new_statement']) or '-':11} {rev['new_statement']}")
        for t in range(1, args.trials + 1):
            r = agents.revise(held, atom, day, scope_ctx)
            n += 1
            tag = shape(r["statement"])
            diary += "DIARY" in tag
            hedge += "HEDGE" in tag
            unchanged += r.get("outcome") == "unchanged"
            print(f"--- trial {t} [{r['kind']}] {r.get('outcome', '')} {tag}\n  {r['statement']}\n  why: {r['reasoning']}")
    print(f"\n{len(revs)} case(s) x {args.trials}: {n} restatements — DIARY {diary}, HEDGE {hedge}, unchanged {unchanged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
