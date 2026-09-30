"""v1 contextual retrieval — a ranked, tag-scoped candidate set of ACTIVE beliefs.

`beliefs_for_context(query=, tags=, k=)` returns active beliefs ranked by
    w_v·relevance (embedding cosine to the query) + w_r·recency + w_f·frequency,
optionally scoped to a tag SET (a consumer's `pull_set` — a belief surfaces if it carries ANY of
the tags). High-recall by design: `status` is the only HARD filter; the tag scope is applied only
when the store is actually tagged (else return all, never nothing). Each returned item carries its
short_id + tags so consumers can cite/route. Reads the live catalog (belief_engine.intake.catalog:
belief_intake_* + belief_tags in emi.db) since the 2026-09-29 cutover; relevance uses the statement
embeddings the intake stored.

There is no structured `applies_when` or `surfacing_log` yet, so temporal and usage ranking terms
are omitted (add them if those land on the store).
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

from belief_engine.db.paths import belief_db_path as _db_path
from belief_engine.tagging import sanitize as _sanitize

_DEFAULT_WEIGHTS = {"relevance": 0.55, "recency": 0.25, "frequency": 0.20}
_RECENCY_HALF_LIFE_DAYS = 30.0
_FREQ_SATURATION = 20.0
_DEFAULT_K = 40


def _default_embedder():
    from app.assistant.embeddings.embedder import embed_texts
    return embed_texts


def _recency(last, now: datetime) -> float:
    if not last:
        return 0.5
    try:
        d = datetime.fromisoformat(str(last))
    except Exception:
        return 0.5
    age = max(0.0, (now.replace(tzinfo=None) - d.replace(tzinfo=None)).total_seconds() / 86400.0)
    return 0.5 ** (age / _RECENCY_HALF_LIFE_DAYS)


def _frequency(n) -> float:
    return min(1.0, math.log1p(float(n or 0)) / math.log1p(_FREQ_SATURATION))


def beliefs_for_context(
    *,
    query: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    k: int = _DEFAULT_K,
    now: Optional[datetime] = None,
    conn=None,
    embedder=None,
    weights: Optional[Dict[str, float]] = None,
    include_scores: bool = False,
) -> List[Dict[str, Any]]:
    """Ranked, optionally tag-scoped candidate set of ACTIVE beliefs (see module docstring)."""
    from belief_engine.intake.catalog import active_entries

    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    now = now or datetime.now(timezone.utc)
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    try:
        entries = active_entries(conn=conn)
        clean = set(_sanitize(tags)) if tags else set()
        # Scope to the tagged slice ONLY when the catalog is actually tagged; on an untagged
        # catalog stay high-recall (return all) instead of returning nothing.
        if clean and any(e["tags"] for e in entries):
            entries = [e for e in entries if clean & set(e["tags"])]
        if not entries:
            return []

        rel = {e["belief_key"]: 0.0 for e in entries}
        if query:
            # The intake stores each statement's embedding when it writes or revises the belief;
            # only the query is embedded here.
            ids = list(rel)
            stored = {}
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                ph = ",".join("?" for _ in chunk)
                for r in conn.execute(f"SELECT id, embedding FROM belief_intake_beliefs WHERE id IN ({ph})", chunk):
                    stored[r[0]] = json.loads(r[1])
            q = (embedder or _default_embedder())([query])[0]
            if np is None:
                raise RuntimeError("numpy is required for belief relevance ranking")
            qv = np.asarray(q, dtype=float)
            qv = qv / (np.linalg.norm(qv) or 1.0)
            for bid, vec in stored.items():
                v = np.asarray(vec, dtype=float)
                rel[bid] = max(0.0, float(qv @ (v / (np.linalg.norm(v) or 1.0))))

        scored: List[Dict[str, Any]] = []
        for e in entries:
            rec = _recency(e["last_confirmed"], now)
            freq = _frequency(e["observation_count"])
            rl = rel[e["belief_key"]]
            item = {
                "id": e["belief_key"], "belief_key": e["belief_key"], "short_id": e["short_id"],
                "statement": e["statement"], "domain": e["domain"], "confidence": e["confidence"],
                "kind": e["kind"], "observation_count": e["observation_count"],
                "last_confirmed": e["last_confirmed"], "tags": sorted(e["tags"]),
                "score": w["relevance"] * rl + w["recency"] * rec + w["frequency"] * freq,
            }
            if include_scores:
                item["_scores"] = {"relevance": rl, "recency": rec, "frequency": freq}
            scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]
    finally:
        if own:
            conn.close()
