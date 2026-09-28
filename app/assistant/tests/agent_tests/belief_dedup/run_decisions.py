"""Re-judge recorded dedup decisions against the store as it stood that day. READ-ONLY.

For every `same` and `contradicts` decision a replay recorded (the verdicts that attach evidence
to a held belief, so the ones a wrong call damages), rebuilds the beliefs held before that day,
re-runs belief_engine::belief_dedup on the same atom, and prints the new verdict/target next to
the recorded one. Nothing is written.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_dedup.run_decisions
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_dedup.run_decisions --verdicts same --store scratch/belief_replay_40d_v6
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.utils.path_utils import get_repo_root
from app.assistant.tests.agent_tests.belief_dedup.run_propagation import store_before
from belief_engine.intake import agents
from belief_engine.intake.rank import ordered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", default="scratch/belief_replay")
    parser.add_argument("--verdicts", nargs="*", default=["same", "contradicts"])
    parser.add_argument("--days", nargs="*", help="only decisions on these days")
    args = parser.parse_args()
    root = get_repo_root()
    store = root / args.store
    c = sqlite3.connect(f"{(store / 'belief_replay.db').resolve().as_uri()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    marks = ",".join("?" * len(args.verdicts))
    decisions = [dict(r) for r in c.execute(f"SELECT * FROM decisions WHERE verdict IN ({marks}) ORDER BY id", args.verdicts)]
    if args.days:
        decisions = [d for d in decisions if d["day"] in args.days]
    from app.assistant.embeddings.embedder import embed_texts
    scope_ctx = agents.scope()
    held_cache: dict[str, list] = {}
    tally = Counter()
    for d in decisions:
        day = d["day"]
        if day not in held_cache:
            held_cache[day] = store_before(c, day)
        held = held_cache[day]
        extract = json.loads((store / "extract" / f"{day}.json").read_text(encoding="utf-8"))
        atom = next(a for a in extract["atoms"] if a["statement"] == d["statement"])
        vec = embed_texts([atom["statement"]])[0]
        verdict = agents.dedup(atom, day, ordered(held, vec), scope_ctx)
        same_call = verdict["verdict"] == d["verdict"] and verdict.get("target") == d["target"]
        tally[f"{d['verdict']} -> {verdict['verdict']}"] += 1
        rec = next((b["statement"] for b in held if b["id"] == d["target"]), "?")
        print("=" * 100)
        print(f"{day}  recorded {d['verdict']} -> {d['target']}: {rec}")
        print(f"   NEW : {atom['statement']}")
        for i, s in enumerate(atom["sources"], 1):
            print(f"        [{i}] {s['kind']} {s['time']}: {(s['text'] or '')[:160]}")
        now = f"{verdict['verdict']}" + (f" -> {verdict['target']}" if verdict.get("target") else "")
        if verdict.get("target") and verdict["target"] != d["target"]:
            now += f": {next((b['statement'] for b in held if b['id'] == verdict['target']), '?')}"
        print(f"   {'AGAIN' if same_call else 'NOW  '}: {now}")
        if verdict.get("sources") is not None:
            print(f"   bearing sources: {verdict['sources']}")
        print(f"   why : {verdict.get('reasoning')}")
    print(f"\n{len(decisions)} decision(s) re-judged: {dict(tally)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
