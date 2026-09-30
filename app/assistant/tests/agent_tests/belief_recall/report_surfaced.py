"""What chat belief recall actually showed the chat gate, turn by turn, from belief_intake_surfaced.
READ-ONLY. Per turn: beliefs shown, characters of the block, relevance-score range, the message.
Totals at the end, so a change to recall can be judged on real turns: fewer characters for the
same answers is the target.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_recall.report_surfaced [--since 2026-09-28]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from belief_engine.db.paths import belief_db_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="")
    args = parser.parse_args()
    c = sqlite3.connect(f"file:{belief_db_path()}?mode=ro", uri=True)
    if not c.execute("SELECT name FROM sqlite_master WHERE name='belief_intake_surfaced'").fetchone():
        print("no surfacing yet")
        return 0
    rows = c.execute(
        "SELECT s.at, s.message_id, s.rank, s.score, s.query, b.statement FROM belief_intake_surfaced s "
        "LEFT JOIN belief_intake_beliefs b ON b.id = s.belief_id WHERE s.at >= ? ORDER BY s.id",
        (args.since,)).fetchall()
    turns: dict[tuple, list] = {}
    for at, mid, rank, score, query, statement in rows:
        turns.setdefault((at, mid, query), []).append((rank, score, statement or "?"))
    total_chars = total_beliefs = 0
    for (at, mid, query), items in turns.items():
        chars = sum(len(f"- {s} (last seen 2026-01-01)") + 1 for _, _, s in items)
        total_chars += chars
        total_beliefs += len(items)
        scores = [sc for _, sc, _ in items]
        print(f"{at}  {len(items)} beliefs  {chars:5} chars  score {min(scores):.2f}–{max(scores):.2f}  "
              f"{query.splitlines()[0][:80]}")
        for rank, score, statement in items:
            print(f"      {rank}. {score:.2f} {statement[:110]}")
    n = len(turns)
    print(f"\n{n} turn(s), {total_beliefs} beliefs, {total_chars} chars"
          + (f" — {total_chars / n:.0f} chars/turn, {total_beliefs / n:.1f} beliefs/turn" if n else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
