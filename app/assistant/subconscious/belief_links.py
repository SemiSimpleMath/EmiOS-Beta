"""The beliefs closest in meaning to what a decider reads (2026-10-09).

Beliefs are memory (docs/design/hub_and_spokes_2026-10-09.md, gap 3): what was learned about the household from what
the owner said and did, each one claim in its own words. Until now they reached decisions only as the day pass's
placements on today's calendar; the brain step and the list decider never read them. On 2026-10-09 the day's record
concluded the owner had not taken the dogs out, not having read that someone else in the household sometimes does.

Found by code, as kg_links finds the knowledge graph's facts: each text is embedded (the local embedder the belief
store's statement embeddings were made with) and every active belief scored against it, best over the texts. The
reader decides what bears on the matter; a belief is context, never an instruction.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

BELIEFS_K = 8
# Cosine (all-MiniLM-L6-v2) below which a belief is not shown. Calibrated on the live store 2026-10-09: beliefs that
# bore on a matter scored 0.27-0.59 ("Did you take the dogs out this morning?" -> the morning dog routine 0.52, and
# someone else sometimes walking them 0.27; "Friday Night Meats is moved" -> its Zoom link 0.59, the walk after it
# 0.56); the best unrelated ones scored 0.18-0.24. A near miss costs a line; a miss costs what the household knows.
MIN_SIMILARITY = 0.25


def _beliefs() -> List[Dict[str, Any]]:
    from belief_engine.intake.run import app_store
    return app_store().beliefs()


def _embed(texts: List[str]) -> List[List[float]]:
    from app.assistant.embeddings.embedder import embed_texts
    return embed_texts(texts)


def beliefs_by_meaning(texts: List[str], *, k: int = BELIEFS_K, min_similarity: float = MIN_SIMILARITY,
                       beliefs: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                       embed: Optional[Callable[[List[str]], List[List[float]]]] = None) -> List[Dict[str, Any]]:
    """The k active beliefs nearest to any of `texts` (cosine, best over the texts), at or above `min_similarity`,
    nearest first: {belief_key, statement, score}. `beliefs` and `embed` are test seams."""
    import numpy as np
    queries = [t for t in texts if str(t or "").strip()]
    if not queries:
        return []
    every = (beliefs or _beliefs)()
    if not every:
        return []
    matrix = np.asarray([b["embedding"] for b in every], dtype=np.float32)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    vectors = np.asarray((embed or _embed)(queries), dtype=np.float32)
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    scores = (vectors @ matrix.T).max(axis=0)
    best = [i for i in np.argsort(-scores)[:k] if scores[i] >= min_similarity]
    return [{"belief_key": every[i]["id"], "statement": every[i]["statement"], "score": round(float(scores[i]), 3)}
            for i in best]
