"""Emergent vocabulary review (#8) — the per-user vocabulary grows itself.

The registry mints new concepts as `candidate` (BELIEF-ENGINE-NEW.md §2b, §7). This periodic
review turns that raw growth into a controlled-but-evolving vocabulary:

  1. MERGE duplicates — a candidate concept that is really an existing one (embedding NN PROPOSES,
     an LLM verifier DECIDES) is folded into the active concept, so future phrases resolve
     consistently. Same candidate→verify pattern as #2/#6; nothing merges on similarity alone.
  2. PROMOTE the proven — a candidate that has recurred (≥ `promote_min_aliases` distinct phrasings)
     graduates to `active`: it has earned a place in the canonical core. One-off candidates stay
     candidates (cheap to keep, easy to prune later).

Deterministic proposes/promotes; the LLM only adjudicates merges. Verifier optional (promotion
alone needs no LLM).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

_CTYPES = ("subject", "predicate", "object", "qualifier")


def _unit(vec):
    v = np.asarray(vec, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def review_vocabulary(
    registry,
    *,
    verifier: Optional[Callable[[str, str, str], bool]] = None,
    promote_min_aliases: int = 2,
    merge_threshold: float = 0.9,
    ctypes=_CTYPES,
) -> Dict[str, Any]:
    """Returns {checked, merged, promoted}. Mutates the registry (merges + promotions)."""
    checked = merged = promoted = 0

    # 1) LLM-verified merge of candidate concepts into a near active concept of the same ctype.
    if verifier is not None and np is not None:
        for ctype in ctypes:
            actives = [c for c in registry.concepts_by_status("active", ctype) if c["embedding_json"]]
            if not actives:
                continue
            amat, avecs = [], []
            for c in actives:
                u = _unit(json.loads(c["embedding_json"]))
                if u is not None:
                    avecs.append(c)
                    amat.append(u)
            if not amat:
                continue
            amat = np.vstack(amat)
            for cand in registry.concepts_by_status("candidate", ctype):
                if not cand["embedding_json"]:
                    continue
                v = _unit(json.loads(cand["embedding_json"]))
                if v is None:
                    continue
                sims = amat @ v
                j = int(sims.argmax())
                if float(sims[j]) >= merge_threshold:
                    checked += 1
                    if verifier(cand["canonical_label"], avecs[j]["canonical_label"], ctype):
                        registry.merge_concepts(cand["concept_id"], avecs[j]["concept_id"], commit=False)
                        merged += 1
        registry.conn.commit()
        registry._load_vectors()

    # 2) Promote candidates that have proven themselves (and weren't just merged away).
    for ctype in ctypes:
        for cand in registry.concepts_by_status("candidate", ctype):
            if registry.alias_count(cand["concept_id"]) >= promote_min_aliases:
                registry.promote(cand["concept_id"], commit=False)
                promoted += 1
    registry.conn.commit()

    return {"checked": checked, "merged": merged, "promoted": promoted}
