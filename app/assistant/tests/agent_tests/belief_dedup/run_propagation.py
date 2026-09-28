"""Re-judge one day's contradicting atom against EVERY other held belief, the way the replay's
contradiction fan-out does. READ-ONLY.

Rebuilds the replay store as it stood before that day (beliefs created earlier, statements as they
were before any later revision), takes the atom the replay recorded as `contradicts` that day, and
walks the fan-out: dedup against the remaining beliefs, and on each further `contradicts` the
revise agent restates that belief. Nothing is written.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_dedup.run_propagation 2026-05-07
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_dedup.run_propagation --all
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.utils.path_utils import get_repo_root
from belief_engine.intake import agents
from belief_engine.intake.propagate import contradiction_fanout


def store_before(conn: sqlite3.Connection, day: str) -> list[dict]:
    """Beliefs as held before `day`: created earlier, with the statement each had at the time."""
    out = []
    for b in conn.execute("SELECT * FROM beliefs WHERE created_day < ? ORDER BY rowid", (day,)):
        b = dict(b)
        b["embedding"] = json.loads(b["embedding"])
        rev = conn.execute("SELECT old_statement, old_kind FROM revisions WHERE belief_id=? AND day>=? ORDER BY id LIMIT 1",
                           (b["id"], day)).fetchone()
        if rev:
            b["statement"], b["kind"] = rev["old_statement"], rev["old_kind"]
        b["sources"] = [dict(e) for e in conn.execute(
            "SELECT day, time, kind, relation, text, in_reply_to FROM evidence WHERE belief_id=? AND day<? ORDER BY id",
            (b["id"], day))]
        out.append(b)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("days", nargs="*")
    parser.add_argument("--all", action="store_true", help="every day the replay recorded a contradiction on")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    root = get_repo_root()
    conn = sqlite3.connect(f"{(root / 'scratch' / 'belief_replay' / 'belief_replay.db').resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    days = args.days
    if args.all:
        days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM decisions WHERE verdict='contradicts' ORDER BY day")]
    if not days:
        raise SystemExit("give days, or --all")
    from app.assistant.embeddings.embedder import embed_texts
    scope_ctx = agents.scope()
    total_hits = 0
    for day in days:
        decisions = conn.execute("SELECT statement, target FROM decisions WHERE day=? AND verdict='contradicts'", (day,)).fetchall()
        if not decisions:
            raise SystemExit(f"no contradicts decision recorded on {day}")
        extract = json.loads((root / "scratch" / "belief_replay" / "extract" / f"{day}.json").read_text(encoding="utf-8"))
        held = store_before(conn, day)
        for d in decisions:
            atom = next(a for a in extract["atoms"] if a["statement"] == d["statement"])
            print("=" * 100)
            print(f"{day} ATOM ({atom['kind']}): {atom['statement']}")
            print(f"recorded target: {d['target']}: {next(b['statement'] for b in held if b['id'] == d['target'])}")
            for t in range(1, args.trials + 1):
                print(f"--- trial {t}")
                hits = contradiction_fanout(atom, day, held, embed_texts([atom["statement"]])[0],
                                            already=[d["target"]], scope_ctx=scope_ctx)
                total_hits += len(hits)
                for bid, revision, verdict in hits:
                    b = next(x for x in held if x["id"] == bid)
                    print(f"  ALSO CONTRADICTS {bid}: {b['statement']}")
                    print(f"      bearing sources {verdict['sources']}; why: {verdict.get('reasoning')}")
                    print(f"      {revision['outcome']} [{revision['kind']}]: {revision['statement']}")
                if not hits:
                    print("  no further belief contradicted")
    print(f"\n{len(days)} day(s), {total_hits} further belief(s) contradicted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
