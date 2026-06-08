"""Seed the v2 observation log from the old belief store snapshot.

The old `belief_evidence` rows ARE observations — they carry source/signal/weight/date.
We replay each as an observation whose identity is OVERRIDDEN to the old belief_key (a
known, persisted resolution), so #1's projection reconstructs the old belief set and we
prove the event-sourcing fold on real, full-history data (6,275 beliefs / 32,306 evidence).

Dedup is NOT this step's job — with the stub/override identity the old keys don't collapse.
#2 (canonical identity) is what shrinks the set; this run establishes the baseline + proves
the log → projection machinery.

Reads `belief_engine_v2/seed/beliefs_seed.db` READ-ONLY; writes its own `data/belief_v2.db`.
Run: .venv/Scripts/python.exe -m belief_engine_v2.seed_from_old
"""
from __future__ import annotations

import os
import sqlite3

from belief_engine_v2.store import Claim, Store

_SEED = "belief_engine_v2/seed/beliefs_seed.db"
_OUT = "belief_engine_v2/data/belief_v2.db"


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

    src = sqlite3.connect(f"file:{_SEED}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT e.source_type, e.source_date, e.signal_type, e.valence, e.weight, "
        "       e.summary, e.raw_text, e.created_at, b.belief_key, b.statement, b.domain, b.status "
        "FROM belief_evidence e JOIN user_beliefs b ON b.id = e.belief_id "
        "ORDER BY e.created_at"
    ).fetchall()

    for r in rows:
        claim = Claim(
            subject=r["domain"] or "",
            predicate=r["belief_key"] or "",        # old key as a coarse predicate handle
            object="",
            statement_nl=r["statement"] or "",
            raw_text=(r["raw_text"] or r["summary"] or ""),
            extra={"old_belief_key": r["belief_key"], "old_status": r["status"], "source_type": r["source_type"]},
        )
        store.append(
            claim,
            source=r["source_type"] or "seed",
            polarity=_polarity(r["signal_type"], r["valence"]),
            occurred_at=(r["source_date"] or r["created_at"]),
            recorded_at=r["created_at"],            # preserve original chronology for the fold
            strength=float(r["weight"]) if r["weight"] is not None else 1.0,
            extractor_version="seed-from-old-v0",
            identity_override=r["belief_key"],      # known resolution (old key)
        )

    n_beliefs = store.rebuild_projection()

    # Baseline comparison
    old_total = src.execute("SELECT COUNT(*) FROM user_beliefs").fetchone()[0]
    old_active = src.execute("SELECT COUNT(*) FROM user_beliefs WHERE status='active'").fetchone()[0]
    with_evidence = src.execute("SELECT COUNT(DISTINCT belief_id) FROM belief_evidence").fetchone()[0]
    v2_active = store.conn.execute("SELECT COUNT(*) FROM beliefs WHERE status='active'").fetchone()[0]
    src.close()

    print(f"observations appended : {len(rows)}")
    print(f"v2 beliefs (projection): {n_beliefs}  (active={v2_active})")
    print(f"old beliefs: total={old_total} active={old_active} with_evidence={with_evidence}")
    print(f"sanity: projection ~ old beliefs-with-evidence ({n_beliefs} vs {with_evidence})")
    print(f"wrote {_OUT}")
    print("NOTE: dedup happens in #2 (canonical identity); this proves log -> deterministic projection.")


if __name__ == "__main__":
    main()
