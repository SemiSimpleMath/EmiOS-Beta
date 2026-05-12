"""Belief decay v2 — model constants + pure math.

Pure functions only. No DB access. The snapshot job in `recompute.py` is
what walks rows and writes back; this module is the math kernel.

True half-life convention: `w × 0.5^(days/half_life)`. At one half-life,
weight is exactly 50% (not 1/e ≈ 37%, which would be the loose-"half-life"
exponential form).

Two-weight model: each evidence event contributes to either support_weight
or contradiction_weight, decaying independently. `net_weight` and
`conflict_ratio` are derived. Band is derived from both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional, Tuple


# --- Belief kinds + half-lives ---------------------------------------------

BELIEF_KINDS = (
    "durable_fact",         # Born in Espoo, has PhD — never decay
    "stable_relationship",  # Married to Katy, brother of Jouko
    "stable_preference",    # Likes mayo, hates mustard
    "routine_pattern",      # Monday 9:15 standup, morning dog walk
    "episodic_context",     # Has a cold this week, on a deadline
    "transient_state",      # Running late, currently in a meeting
)

DEFAULT_KIND = "routine_pattern"

# half-life in days. None means "no decay" (durable_fact only).
HALF_LIFE_DAYS = {
    "durable_fact":        None,
    "stable_relationship": 1825,   # 5 years
    "stable_preference":    365,   # 1 year
    "routine_pattern":       90,   # 3 months
    "episodic_context":      14,   # 2 weeks
    "transient_state":        1,   # 1 day
}


# --- Evidence weight calibration -------------------------------------------

# Base weight per source_type. Tunable. These match the rough weights
# already emitted by collect_evidence.py for parity with existing data.
EVIDENCE_BASE_WEIGHT = {
    "daily_insights":     1.5,   # extracted from chat
    "ticket_acceptance":  2.0,   # user accepted a suggestion
    "ticket_rejection":   2.5,   # user explicitly declined — stronger
    "kg_edge":            1.0,   # implicit observation
    "manual_seed":        3.0,   # explicit user statement
    "canonicalization":   0.0,   # bookkeeping events
    "deprecation":        0.0,   # bookkeeping
}
DEFAULT_BASE_WEIGHT = 1.0


# --- Band thresholds -------------------------------------------------------

# Confidence band derived from (support, contradiction, net) weights.
# Tunable. Initial values per the 2026-05-11 design discussion.
@dataclass(frozen=True)
class BandThresholds:
    # If contradiction dominates this fraction of total, mark as
    # deprecated_by_contradiction (terminal). Conflict_ratio = c / (s + c).
    deprecated_by_contradiction_ratio: float = 0.6
    # If both sides are high, mark contested (route to reevaluator).
    contested_support_min: float = 2.0
    contested_contradiction_min: float = 2.0
    # Otherwise derive from net_weight = support - contradiction.
    high_min: float = 4.0
    medium_min: float = 1.5
    low_min: float = 0.3
    # Below low_min → faded (terminal).


BAND_THRESHOLDS = BandThresholds()


# --- Valence mapping -------------------------------------------------------

Valence = Literal["support", "contradict", "qualify"]


def valence_from_signal_type(signal_type: str) -> Valence:
    """Map collect_evidence's signal_type vocabulary to a valence.

    confirms → support
    qualifies → qualify (still counts as support but tagged)
    rejects → contradict
    contradicts → contradict
    Anything else → support (defensive)
    """
    s = (signal_type or "").strip().lower()
    if s in ("contradicts", "rejects"):
        return "contradict"
    if s == "qualifies":
        return "qualify"
    return "support"


# --- Math helpers ----------------------------------------------------------

def half_life_for_kind(kind: Optional[str]) -> Optional[int]:
    """Return half-life in days for a kind; None for no-decay durable_fact."""
    if kind is None:
        return HALF_LIFE_DAYS.get(DEFAULT_KIND)
    return HALF_LIFE_DAYS.get(kind, HALF_LIFE_DAYS[DEFAULT_KIND])


def decayed_weight(
    base_weight: float,
    age_days: float,
    half_life_days: Optional[int],
) -> float:
    """Apply true half-life decay: w × 0.5^(age/half_life).

    `half_life_days=None` means no decay (durable facts). Negative ages are
    clamped to zero so future-dated evidence doesn't get a bonus.
    """
    if half_life_days is None:
        return float(base_weight)
    age = max(0.0, float(age_days))
    return float(base_weight) * (0.5 ** (age / float(half_life_days)))


@dataclass(frozen=True)
class BeliefWeights:
    """Snapshot of an aggregated belief's evidence-weighted state."""
    support_weight: float
    contradiction_weight: float
    net_weight: float
    conflict_ratio: float


def compute_belief_weights(
    evidence: Iterable[Tuple[float, str, datetime]],
    kind: Optional[str],
    now: datetime,
) -> BeliefWeights:
    """Aggregate decayed support/contradict weights for a belief.

    `evidence` is an iterable of (base_weight, valence, observed_at).
    Valence 'qualify' counts as support (light positive). 'contradict'
    counts as contradiction. 'support' counts as support.

    Returns BeliefWeights with `conflict_ratio` ∈ [0, 1].
    """
    half_life = half_life_for_kind(kind)
    support_total = 0.0
    contradiction_total = 0.0

    for base_w, valence, observed_at in evidence:
        if not observed_at:
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_days = (now - observed_at).total_seconds() / 86400.0
        contrib = decayed_weight(base_w or 0.0, age_days, half_life)
        if (valence or "").lower() == "contradict":
            contradiction_total += contrib
        else:  # 'support' or 'qualify' (or unknown defaults to support)
            support_total += contrib

    net = support_total - contradiction_total
    total = support_total + contradiction_total
    conflict_ratio = (contradiction_total / total) if total > 0 else 0.0
    return BeliefWeights(
        support_weight=support_total,
        contradiction_weight=contradiction_total,
        net_weight=net,
        conflict_ratio=conflict_ratio,
    )


# --- Band classification ---------------------------------------------------

Band = Literal["high", "medium", "low", "faded", "contested", "deprecated_by_contradiction"]


def band_for_weights(
    weights: BeliefWeights,
    *,
    thresholds: BandThresholds = BAND_THRESHOLDS,
) -> Band:
    """Map (support, contradiction) → confidence band.

    Order of checks matters:
      1. contradiction dominates fraction of total → deprecated_by_contradiction
      2. both high → contested (math hands off to reevaluator)
      3. otherwise net_weight → high/medium/low/faded bands
    """
    if weights.conflict_ratio >= thresholds.deprecated_by_contradiction_ratio:
        return "deprecated_by_contradiction"
    if (
        weights.support_weight >= thresholds.contested_support_min
        and weights.contradiction_weight >= thresholds.contested_contradiction_min
    ):
        return "contested"
    net = weights.net_weight
    if net >= thresholds.high_min:
        return "high"
    if net >= thresholds.medium_min:
        return "medium"
    if net >= thresholds.low_min:
        return "low"
    return "faded"


# --- Heuristic kind classifier (for backfilling existing beliefs) ----------

# Belief_key prefix or substring → kind. Highest-priority match wins.
# These match observed patterns in the current 3,605-row belief store.
_KEY_TO_KIND = (
    ("identity.",       "durable_fact"),
    ("biographical.",   "durable_fact"),
    ("family.",         "stable_relationship"),
    ("relationship.",   "stable_relationship"),
    ("contact.",        "stable_relationship"),
    ("preference.",     "stable_preference"),
    ("dietary.",        "stable_preference"),
    ("food.",           "stable_preference"),
    ("communication.",  "stable_preference"),
    ("routine.",        "routine_pattern"),
    ("schedule.",       "routine_pattern"),
    ("work.",           "routine_pattern"),
    ("health.",         "stable_preference"),  # health-related rules are mostly preferences
)

# Domain fallback when key doesn't match any prefix.
_DOMAIN_TO_KIND = {
    "routine":       "routine_pattern",
    "health":        "stable_preference",
    "food":          "stable_preference",
    "communication": "stable_preference",
    "sleep":         "stable_preference",
    "work":          "routine_pattern",
    "general":       "stable_preference",
}


def classify_kind_heuristic(
    *,
    belief_key: str,
    domain: str,
    scope: Optional[str],
) -> str:
    """Heuristic backfill for existing beliefs without a `kind` value.

    Priority:
      1. Scope = temporary → episodic_context (always)
      2. Key prefix match against _KEY_TO_KIND
      3. Domain fallback
      4. DEFAULT_KIND
    """
    if (scope or "").lower() == "temporary":
        return "episodic_context"
    k = (belief_key or "").lower()
    for prefix, kind in _KEY_TO_KIND:
        if k.startswith(prefix):
            return kind
    return _DOMAIN_TO_KIND.get((domain or "").lower(), DEFAULT_KIND)
