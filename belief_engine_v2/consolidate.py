"""Consolidation (#6) — the concept/belief-to-belief merge the write-time pass can't do.

A single greedy write-time resolution never compares two EXISTING beliefs to each other, so
duplicates that formed separately coexist (the demo's 8+6 "weekly timesheet" concepts). This
periodic sweep fixes that exactly as #2 resolves phrases: embedding NN PROPOSES candidate belief
pairs, an LLM verifier DECIDES "same belief?", and a verified merge is recorded as a REVERSIBLE
redirect (store.merge_beliefs) whose justifications union on the next fold. Deterministic proposes
and ranks; the LLM decides; a non-match is left alone — nothing is merged on similarity alone
(see [[feedback_deterministic_proposes_llm_decides]]).

Operates on the beliefs PROJECTION, so run it after rebuild_projection(). Embedder + verifier are
injected (same shapes as the registry's), so tests run with fakes and no model/LLM.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


def consolidate(
    store,
    *,
    embedder: Callable[[str], List[float]],
    verifier: Callable[[str, str, str], bool],
    threshold: float = 0.86,
    max_pairs: int = 500,
    statuses=("active", "contested"),
    require_same_predicate: bool = True,
    pair_same_subject_predicate: bool = False,
) -> Dict[str, Any]:
    """Merge duplicate beliefs. Returns {candidates, verified, merges}. Mutates via redirects +
    a final rebuild. `require_same_predicate` blocks cross-predicate merges when predicates are
    known (they're empty in the whole-statement seed, so it no-ops there).

    Recall has TWO sources (the verifier still decides every pair — nothing merges on recall alone):
    (a) embedding NN ≥ threshold, and (b) `pair_same_subject_predicate` — beliefs sharing the same
    canonical subject+predicate. (b) is the load-bearing one for STRUCTURED beliefs: two phrasings of
    the same preference ("standing-break nudges" vs "standing break reminders") embed at only ~0.6 but
    share subject+predicate, so embedding alone would never propose them. It no-ops on the seed (empty
    subject/predicate)."""
    if np is None:
        raise RuntimeError("consolidate requires numpy")

    placeholders = ",".join("?" for _ in statuses)
    rows = store.conn.execute(
        f"SELECT belief_id, statement_nl, subject, predicate, support FROM beliefs "
        f"WHERE status IN ({placeholders})", tuple(statuses),
    ).fetchall()
    items = [r for r in rows if (r["statement_nl"] or "").strip()]
    if len(items) < 2:
        return {"candidates": 0, "verified": 0, "merges": 0}

    mat = np.asarray([embedder(r["statement_nl"]) for r in items], dtype=float)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms > 0, norms, 1.0)
    sims = mat @ mat.T

    # Candidate pairs (proposals only — the verifier decides each): i<j that are EITHER embedding-near
    # (sim ≥ threshold) OR share the same canonical subject+predicate. Strongest first.
    pairs = []
    n = len(items)
    for i in range(n):
        pi, si = items[i]["predicate"] or "", items[i]["subject"] or ""
        for j in range(i + 1, n):
            s = float(sims[i, j])
            pj, sj = items[j]["predicate"] or "", items[j]["subject"] or ""
            same_sp = bool(pair_same_subject_predicate and si and pi and si == sj and pi == pj)
            if s < threshold and not same_sp:
                continue
            if require_same_predicate and pi and pj and pi != pj:
                continue
            pairs.append((s, i, j))
    pairs.sort(reverse=True)
    pairs = pairs[:max_pairs]

    support = {r["belief_id"]: (r["support"] or 0.0) for r in items}
    stmt = {r["belief_id"]: r["statement_nl"] for r in items}
    bid = {idx: items[idx]["belief_id"] for idx in range(n)}

    merged_into: Dict[str, str] = {}                  # local union map for this sweep

    def root(x: str) -> str:
        seen = set()
        while x in merged_into and x not in seen:
            seen.add(x)
            x = merged_into[x]
        return x

    verified = merges = 0
    for s, i, j in pairs:
        a, b = root(bid[i]), root(bid[j])
        if a == b:
            continue
        if verifier(stmt[a], stmt[b], "belief"):       # the LLM decides the merge
            verified += 1
            survivor, loser = (a, b) if support[a] >= support[b] else (b, a)
            store.merge_beliefs(loser, survivor,
                                detail={"similarity": round(s, 4), "loser_stmt": stmt[loser]},
                                commit=False)
            merged_into[loser] = survivor
            merges += 1
    store.conn.commit()
    store.rebuild_projection()
    return {"candidates": len(pairs), "verified": verified, "merges": merges}
