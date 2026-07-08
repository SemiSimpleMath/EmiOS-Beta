"""v1 contextual retrieval — a ranked, tag-scoped candidate set of ACTIVE beliefs.

`beliefs_for_context(query=, tags=, k=)` returns active user_beliefs ranked by
    w_v·relevance (embedding cosine to the query) + w_r·recency + w_f·frequency,
optionally scoped to a tag SET (a consumer's `pull_set` — a belief surfaces if it carries ANY of
the tags). High-recall by design: `status` is the only HARD filter; the tag scope is applied only
when the store is actually tagged (else return all, never nothing). Each returned item carries its
short_id + tags so consumers can cite/route. Reads the v1 store (emi.db: user_beliefs +
belief_tags + belief_short_id).

There is no structured `applies_when` or `surfacing_log` yet, so temporal and usage ranking terms
are omitted (add them if those land on the store).
"""
from __future__ import annotations

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
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    now = now or datetime.now(timezone.utc)
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    try:
        where, params = "b.status='active'", []
        clean = _sanitize(tags) if tags else []
        # Scope to the tagged slice ONLY when the store is actually tagged; on an untagged store
        # stay high-recall (return all) instead of returning nothing.
        if clean and conn.execute("SELECT 1 FROM belief_tags LIMIT 1").fetchone():
            ph = ",".join("?" for _ in clean)
            where += f" AND b.id IN (SELECT belief_id FROM belief_tags WHERE tag IN ({ph}))"
            params = clean
        rows = conn.execute(
            "SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence, b.kind, "
            "b.observation_count, b.last_confirmed, s.short_id "
            "FROM user_beliefs b LEFT JOIN belief_short_id s ON s.belief_id=b.id "
            f"WHERE {where}", params).fetchall()
        if not rows:
            return []

        ids = [r["id"] for r in rows]
        tags_by_id: Dict[str, List[str]] = {i: [] for i in ids}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ",".join("?" for _ in chunk)
            for tr in conn.execute(f"SELECT belief_id, tag FROM belief_tags WHERE belief_id IN ({ph})", chunk):
                tags_by_id.setdefault(tr[0], []).append(tr[1])

        rel = {i: 0.0 for i in ids}
        if query:
            emb = embedder or _default_embedder()
            vecs = emb([query] + [r["statement"] or "" for r in rows])
            if np is not None and vecs:
                M = np.asarray(vecs, dtype=float)
                norms = np.linalg.norm(M, axis=1, keepdims=True)
                M = M / np.where(norms > 0, norms, 1.0)
                q = M[0]
                for idx, r in enumerate(rows):
                    rel[r["id"]] = max(0.0, float(q @ M[idx + 1]))

        scored: List[Dict[str, Any]] = []
        for r in rows:
            rec = _recency(r["last_confirmed"], now)
            freq = _frequency(r["observation_count"])
            rl = rel[r["id"]]
            item = {
                "id": r["id"], "belief_key": r["belief_key"],
                "short_id": f"b{r['short_id']}" if r["short_id"] is not None else None,
                "statement": r["statement"], "domain": r["domain"], "confidence": r["confidence"],
                "kind": r["kind"], "observation_count": r["observation_count"],
                "last_confirmed": r["last_confirmed"],
                "tags": sorted(tags_by_id.get(r["id"], [])),
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
