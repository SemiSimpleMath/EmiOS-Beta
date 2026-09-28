"""Order held beliefs by closeness in meaning to a vector. Ranking only; nothing is dropped."""
from __future__ import annotations


def ordered(beliefs: list[dict], vector: list) -> list[dict]:
    if not beliefs:
        return []
    import numpy as np
    M = np.asarray([b["embedding"] for b in beliefs], dtype=float)
    q = np.asarray(vector, dtype=float)
    sims = (M @ q) / np.maximum(np.linalg.norm(M, axis=1) * np.linalg.norm(q), 1e-12)
    return [beliefs[i] for i in np.argsort(-sims)]
