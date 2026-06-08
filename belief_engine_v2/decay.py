"""Cadence-aware survival decay (#5) — absence ≠ negative.

Silence is EXPECTED for evergreen and periodic beliefs, so it must not erode them. An
observation's effective weight is its raw weight × `survival(kind, age, interval)`; #4's status is
then derived from those decayed weights (BELIEF-ENGINE-NEW.md §4). Crucially, silence decays a
belief's STRENGTH but never adds NEGATIVE evidence — absence is not a contradiction.

  semantic_fact / preference / None : evergreen — survival = 1.0 at any age (only a contradiction,
                                      via #4, can lower it).
  procedural_routine                : decays by MISSED expected intervals — one full interval of
                                      grace, then a half-life of one interval. The yearly cake
                                      survives 11 months of silence; a weekly routine silent 3
                                      weeks is down to a quarter.
  episodic                          : wall-clock half-life in weeks.
  transient_state                   : wall-clock half-life in days.

Deterministic and pure (numbers in, number out); the caller supplies `age` and `interval`.
Later (premature now): power-law/ACT-R retention + FSRS stability instead of these fixed rules.
"""
from __future__ import annotations

from typing import Optional

_EPISODIC_HALFLIFE_D = 21.0    # episodic memories fade over a few weeks
_TRANSIENT_HALFLIFE_D = 3.0    # transient states fade over days
_DEFAULT_ROUTINE_INTERVAL_D = 30.0   # routine with no derivable cadence → assume monthly

_RECURRENCE_DAYS = {"P1D": 1.0, "P1W": 7.0, "P1M": 30.0, "P1Y": 365.0}

_EVERGREEN_KINDS = {"", "semantic_fact", "preference"}


def survival(kind: Optional[str], age_days: float, *, interval_days: Optional[float] = None) -> float:
    """Effective-weight multiplier in (0, 1] for an observation of `age_days`, by belief kind."""
    age = max(0.0, float(age_days))
    k = (kind or "").strip().lower()

    if k in _EVERGREEN_KINDS:
        return 1.0                                  # silence is not evidence against an evergreen
    if k == "procedural_routine":
        iv = interval_days or _DEFAULT_ROUTINE_INTERVAL_D
        missed = max(0.0, age / iv - 1.0)           # one interval of grace, then decay
        return 0.5 ** missed
    if k == "episodic":
        return 0.5 ** (age / _EPISODIC_HALFLIFE_D)
    if k == "transient_state":
        return 0.5 ** (age / _TRANSIENT_HALFLIFE_D)
    return 1.0                                      # unknown kind → don't decay (fail-open)


def interval_from_cue(applies_when: Optional[dict]) -> Optional[float]:
    """Best-effort expected interval (days) for a routine, read from its temporal cue."""
    if not applies_when:
        return None
    if applies_when.get("anchor"):                  # e.g. first_business_day → monthly
        return 30.0
    if applies_when.get("weekday"):
        return 7.0
    if any(applies_when.get(k) is not None for k in ("at", "after", "before")):
        return 1.0                                  # a daily setpoint
    return None


def interval_from_spacing(sorted_ages_days: list) -> Optional[float]:
    """Median gap between consecutive observation ages (days). Needs ≥2 observations."""
    xs = sorted(float(a) for a in sorted_ages_days)
    if len(xs) < 2:
        return None
    gaps = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    return median if median > 0 else None
