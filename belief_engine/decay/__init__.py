"""Belief decay v2 — evidence-weighted half-life model.

See `belief_engine/decay/model.py` for the math + constants and
`belief_engine/decay/recompute.py` for the snapshot job.

Design summary:
  - Two weights: support_weight + contradiction_weight (not one signed scalar).
  - True half-life: weight × 0.5^(days/half_life). At one half-life, weight = 50%.
  - `kind` field on beliefs drives half-life (durable_fact = no decay).
  - Confidence band is computed and snapshotted, not read-time.
  - Ambiguity (contested, contradiction-vs-durable, replacement candidates)
    routes to the LLM reevaluator; math handles routine aging on its own.
"""
from belief_engine.decay.model import (
    BELIEF_KINDS,
    DEFAULT_KIND,
    HALF_LIFE_DAYS,
    EVIDENCE_BASE_WEIGHT,
    BAND_THRESHOLDS,
    valence_from_signal_type,
    half_life_for_kind,
    decayed_weight,
    compute_belief_weights,
    band_for_weights,
    classify_kind_heuristic,
)

__all__ = [
    "BELIEF_KINDS",
    "DEFAULT_KIND",
    "HALF_LIFE_DAYS",
    "EVIDENCE_BASE_WEIGHT",
    "BAND_THRESHOLDS",
    "valence_from_signal_type",
    "half_life_for_kind",
    "decayed_weight",
    "compute_belief_weights",
    "band_for_weights",
    "classify_kind_heuristic",
]
