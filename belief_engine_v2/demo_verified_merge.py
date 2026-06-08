"""Bounded real-LLM demo of verified merging (#2, scratch/BELIEF-ENGINE-NEW.md §5).

Proves on the timesheet subset the two things a deterministic threshold cannot do safely at
once — because the LLM, not a cosine cutoff, makes every merge decision:
  - duplicate phrasings of the SAME belief collapse to one concept (verifier says "yes"), and
  - weekly vs monthly timesheet beliefs stay SEPARATE (verifier says "no" on the cadence).

τ is set generously (high recall) on purpose: the embedding hands the verifier MORE candidates,
and the verifier draws the line. Tightening τ would only hide candidates from it.

Reads beliefs_seed.db READ-ONLY; uses the app embedder + a real LLM verifier (cheap model).
Run: .venv/Scripts/python.exe -m belief_engine_v2.demo_verified_merge
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from belief_engine_v2.merge_verifier import LLMMergeVerifier
from belief_engine_v2.registry import Registry

_SEED = "belief_engine_v2/seed/beliefs_seed.db"
_TAU = 0.80   # generous recall: surface more candidates; the verifier — not τ — decides merges


def _bucket(s: str) -> str:
    s = s.lower()
    return "weekly" if "week" in s else "monthly" if "month" in s else "other"


def main() -> None:
    from app.assistant.embeddings.embedder import embed_texts

    src = sqlite3.connect(f"file:{_SEED}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT DISTINCT statement FROM user_beliefs "
        "WHERE statement LIKE '%timesheet%' AND statement IS NOT NULL ORDER BY statement"
    ).fetchall()
    src.close()
    stmts = [r["statement"] for r in rows]
    print(f"timesheet distinct statements: {len(stmts)}")
    if not stmts:
        return

    vecs = embed_texts(stmts)
    vd = dict(zip(stmts, vecs))
    emb = lambda t: vd.get(t, [0.0] * len(vecs[0]))   # noqa: E731

    conn = sqlite3.connect(":memory:")
    verifier = LLMMergeVerifier(engine="gpt-5.4-mini")
    reg = Registry(conn, embedder=emb, verifier=verifier, threshold=_TAU, max_candidates=8)

    groups = defaultdict(list)
    for s in stmts:
        cid = reg.resolve(s, "object")
        groups[cid].append(s)

    print(f"-> {len(groups)} canonical concepts from {len(stmts)} statements "
          f"({verifier.calls} LLM verifications)")
    for cid, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cadences = "/".join(sorted({_bucket(m) for m in members}))
        print(f"\n  concept {cid}  [{cadences}]  ({len(members)} statements):")
        for m in members:
            print(f"     - {m[:90]}")

    mixed = [cid for cid, members in groups.items()
             if {"weekly", "monthly"} <= {_bucket(m) for m in members}]
    print(f"\n  concepts that merged weekly+monthly into one (MUST be 0): {len(mixed)}")


if __name__ == "__main__":
    main()
