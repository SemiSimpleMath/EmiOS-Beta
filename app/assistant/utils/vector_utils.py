"""Shared vector math for embedding comparisons.

One permissive ``cosine_similarity`` (None/empty/zero-norm input → 0.0)
used by KG merge scoring, KG search, and semantic tool matching — was
copy-pasted three times (dedup audit 2026-06-10). The strict, raising
variant in ``user_bio_context_service`` is a deliberate separate
contract (fail-loud on dimension mismatch), not a duplicate.
"""
from __future__ import annotations

import numpy as np


def cosine_similarity(vec1, vec2) -> float:
    if vec1 is None or vec2 is None:
        return 0.0
    v1 = np.array(vec1)
    v2 = np.array(vec2)
    if v1.size == 0 or v2.size == 0:
        return 0.0
    denom = np.linalg.norm(v1) * np.linalg.norm(v2)
    if denom == 0:
        return 0.0
    return float(np.dot(v1, v2) / denom)
