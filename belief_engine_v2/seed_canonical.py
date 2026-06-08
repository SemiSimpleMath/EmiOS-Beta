"""Seed with CANONICAL identity (#2), no-verifier SAFETY baseline + verifier workload.

Per principle 5, embeddings only PROPOSE merge candidates; an LLM must verify before any
merge. With no verifier injected the resolver therefore mints every distinct concept (never
auto-merges) — that is the safe baseline this script measures. It then sizes the *verifier
workload*: at each recall threshold τ, how many statements have a near neighbor (so a real
verified pass would ask the LLM "same?") and roughly how many candidate pairs that is. The
actual collapse only happens once the verifier says yes — see demo_verified_merge.py for a
bounded real-LLM pass.

For the seed (old data has no clean subject/predicate/object), we canonicalize the belief
STATEMENT as the object; live observations from the LLM extractor carry full s/p/o.

Reads beliefs_seed.db READ-ONLY; uses the app's embedder; writes its own db.
Run: .venv/Scripts/python.exe -m belief_engine_v2.seed_canonical
"""
from __future__ import annotations

import os
import sqlite3

from belief_engine_v2.registry import Registry
from belief_engine_v2.resolver import Resolver
from belief_engine_v2.store import Claim, Store

_SEED = "belief_engine_v2/seed/beliefs_seed.db"
_OUT = "belief_engine_v2/data/belief_v2_canonical.db"
_TAU = 0.86


def _polarity(signal_type, valence) -> str:
    v = (valence or "").strip().lower()
    s = (signal_type or "").strip().lower()
    return "contradict" if (v == "contradict" or s in ("contradicts", "rejects")) else "affirm"


def main() -> None:
    from app.assistant.embeddings.embedder import embed_texts
    import numpy as np

    src = sqlite3.connect(f"file:{_SEED}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT e.source_type,e.source_date,e.signal_type,e.valence,e.weight,e.summary,e.raw_text,"
        "e.created_at,b.statement FROM belief_evidence e JOIN user_beliefs b ON b.id=e.belief_id "
        "ORDER BY e.created_at"
    ).fetchall()

    distinct = sorted({r["statement"] for r in rows if r["statement"]})
    print(f"distinct statements to embed: {len(distinct)}")
    vecs = embed_texts(distinct)
    dim = len(vecs[0]) if vecs else 1
    vd = dict(zip(distinct, vecs))
    emb = lambda t: vd.get(t, [0.0] * dim)   # noqa: E731

    os.makedirs("belief_engine_v2/data", exist_ok=True)
    if os.path.exists(_OUT):
        os.remove(_OUT)
    store = Store(_OUT)
    reg = Registry(store.conn, embedder=emb, threshold=_TAU)   # NO verifier => never auto-merges
    store.resolver = Resolver(reg)

    for r in rows:
        store.append(
            Claim(subject="", predicate="", object=r["statement"] or "",
                  statement_nl=r["statement"] or "", raw_text=(r["raw_text"] or r["summary"] or "")),
            source=r["source_type"] or "seed",
            polarity=_polarity(r["signal_type"], r["valence"]),
            occurred_at=(r["source_date"] or r["created_at"]),
            recorded_at=r["created_at"],
            strength=float(r["weight"]) if r["weight"] is not None else 1.0,
            extractor_version="seed-canonical-v0",
            commit=False,
        )
    store.conn.commit()
    nb = store.rebuild_projection()

    print(f"SAFETY BASELINE @tau={_TAU} (no verifier => no merges): "
          f"{len(rows)} observations -> {nb} beliefs")
    print("  every merge now requires LLM verification; embedding only proposes candidates.")

    # Verifier workload: at each recall tau, how many distinct statements have a near neighbor
    # (=> at least one LLM 'same?' check), and roughly how many candidate pairs that is. This is
    # the LLM cost of a real verified consolidation pass (capped per statement by max_candidates).
    M = np.asarray([vd[s] for s in distinct], dtype=np.float32)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    sim = M @ M.T
    np.fill_diagonal(sim, -1.0)
    print("  verifier workload (LLM 'same?' checks a verified pass would need):")
    for tau in (0.80, 0.84, 0.88, 0.92):
        above = sim >= tau
        stmts_with_candidate = int(above.any(axis=1).sum())
        pairs = int(above.sum() // 2)
        print(f"     tau={tau}: {stmts_with_candidate}/{len(distinct)} statements have a candidate"
              f"  (up to ~{pairs} candidate pairs to verify)")
    src.close()
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
