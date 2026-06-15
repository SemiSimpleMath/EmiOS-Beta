"""Faithful seed of the v2 observation log from the live v1 belief store (emi.db).

The old `belief_evidence` rows ARE observations — each carries source / signal /
weight / date. We replay every row of every v1 ACTIVE belief as an observation,
preserving the full lifecycle so the v2 projection reconstructs v1's beliefs with
real history (not a flattened pilot):
  - occurred_at = source_date          (real valid-time → recency + decay work)
  - recorded_at = created_at           (original chronology for the fold)
  - polarity    = signal/valence       (confirms→affirm; contradicts/rejects→contradict)
  - strength    = weight
  - source      = source_type          (the channel: daily_insights, ticket_*, user_comment)
  - kind        = mapped v1 kind        (drives survival decay)
Identity is OVERRIDDEN to the old belief_key (a known, persisted resolution), so each
v1 belief becomes one v2 belief with its full justification set. Only v1 ACTIVE beliefs
are seeded — deprecated/merged ones are not resurrected. Dedup is a SEPARATE step (the
lexical + LLM bootstrap merge) run on this faithful base.

Reads emi.db READ-ONLY; writes belief_engine_v2/data/belief_v2_faithful.db.
Run: .venv/Scripts/python.exe -m belief_engine_v2.seed_from_old
"""
from __future__ import annotations

import os
import sqlite3

from belief_engine_v2.store import Claim, Store

_SRC = "emi.db"
_OUT = "belief_engine_v2/data/belief_v2_faithful.db"

# v1 `kind` -> v2 `kind`. v2 decay: semantic_fact/preference = evergreen (no silence
# decay), procedural_routine = interval decay, episodic = short half-life.
_KIND_MAP = {
    "durable_fact": "semantic_fact",
    "stable_relationship": "semantic_fact",
    "stable_preference": "preference",
    "routine_pattern": "procedural_routine",
    "episodic_context": "episodic",
    "transient_state": "transient_state",
}


def _polarity(signal_type, valence) -> str:
    v = (valence or "").strip().lower()
    s = (signal_type or "").strip().lower()
    if v == "contradict" or s in ("contradicts", "rejects"):
        return "contradict"
    return "affirm"


def main() -> None:
    os.makedirs("belief_engine_v2/data", exist_ok=True)
    if os.path.exists(_OUT):
        os.remove(_OUT)
    store = Store(_OUT)

    src = sqlite3.connect(f"file:{_SRC}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT e.source_type, e.source_date, e.signal_type, e.valence, e.weight, "
        "       e.summary, e.raw_text, e.created_at, "
        "       b.belief_key, b.statement, b.domain, b.status, b.kind "
        "FROM belief_evidence e JOIN user_beliefs b ON b.id = e.belief_id "
        "WHERE b.status = 'active' "
        "ORDER BY e.created_at"
    ).fetchall()

    for r in rows:
        w = r["weight"]
        claim = Claim(
            subject=r["domain"] or "",
            predicate=r["belief_key"] or "",          # old key as a coarse identity handle
            object="",
            statement_nl=r["statement"] or "",
            raw_text=(r["raw_text"] or r["summary"] or ""),
            extra={
                "old_belief_key": r["belief_key"],
                "old_status": r["status"],
                "source_type": r["source_type"],
                "kind": _KIND_MAP.get((r["kind"] or "").strip().lower()),
            },
        )
        store.append(
            claim,
            source=r["source_type"] or "seed",
            polarity=_polarity(r["signal_type"], r["valence"]),
            occurred_at=(r["source_date"] or r["created_at"]),
            recorded_at=r["created_at"],               # preserve original chronology for the fold
            strength=float(w) if w is not None else 1.0,
            extractor_version="seed-from-old-v1-kind",
            identity_override=r["belief_key"],         # known resolution (old key)
        )

    n = store.rebuild_projection()
    src_active = src.execute("SELECT COUNT(*) FROM user_beliefs WHERE status='active'").fetchone()[0]
    by_status = dict(store.conn.execute("SELECT status, COUNT(*) FROM beliefs GROUP BY status").fetchall())
    by_pol = dict(store.conn.execute("SELECT polarity, COUNT(*) FROM observations GROUP BY polarity").fetchall())
    with_kind = store.conn.execute("SELECT COUNT(*) FROM beliefs WHERE kind IS NOT NULL").fetchone()[0]
    drange = store.conn.execute("SELECT min(occurred_at), max(occurred_at), "
                                "count(distinct substr(occurred_at,1,10)) FROM observations").fetchone()
    src.close()
    print(f"observations appended  : {len(rows)}")
    print(f"v2 beliefs (projection): {n}  (v1 active beliefs: {src_active})")
    print(f"  by status      : {by_status}")
    print(f"  obs polarity   : {by_pol}")
    print(f"  with kind      : {with_kind}/{n}")
    print(f"  occurred dates : {drange[0]} .. {drange[1]} ({drange[2]} distinct)")
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
