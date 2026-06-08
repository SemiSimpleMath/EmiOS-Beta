"""#5 cadence-aware survival decay: absence ≠ negative. Evergreen beliefs never decay on
silence; a routine decays only once OVERDUE and eventually fades to dormant — never by adding
negative evidence. No model load."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from belief_engine_v2.decay import interval_from_cue, survival
from belief_engine_v2.store import Claim, Store

_NOW = datetime(2026, 6, 8, 12, 0)


def _store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _add(st, *, kind=None, applies_when=None, age_days=0.0, strength=1.0, polarity="affirm"):
    occ = (_NOW - timedelta(days=age_days)).isoformat()
    return st.append(
        Claim(subject="alex", predicate="does_routine", object="thing", statement_nl="thing",
              applies_when=applies_when, extra=({"kind": kind} if kind else {})),
        source="seed", occurred_at=occ, recorded_at=occ, strength=strength, polarity=polarity)


# ── survival curve (unit) ────────────────────────────────────────────────────
def test_survival_curve():
    for k in (None, "semantic_fact", "preference"):
        assert survival(k, 400) == 1.0                      # evergreen: silence never decays
    assert survival("unknown_kind", 999) == 1.0             # fail-open

    # procedural routine: one interval of grace, then half-life of one interval
    assert survival("procedural_routine", 5, interval_days=7) == 1.0      # within the week
    assert survival("procedural_routine", 14, interval_days=7) == 0.5     # one interval overdue
    assert survival("procedural_routine", 21, interval_days=7) == 0.25    # two intervals overdue

    # the yearly cake survives 11 months of silence, halves by two years
    assert survival("procedural_routine", 330, interval_days=365) == 1.0
    assert survival("procedural_routine", 730, interval_days=365) == 0.5

    assert survival("episodic", 21) == 0.5                  # ~3-week half-life
    assert survival("transient_state", 3) == 0.5           # ~3-day half-life


def test_interval_from_cue():
    assert interval_from_cue({"weekday": "MON"}) == 7.0
    assert interval_from_cue({"anchor": "first_business_day"}) == 30.0
    assert interval_from_cue({"at": "21:00"}) == 1.0
    assert interval_from_cue(None) is None


# ── through the fold ─────────────────────────────────────────────────────────
def test_evergreen_does_not_decay_on_silence():
    st = _store()
    _add(st, kind="preference", age_days=400)               # silent for over a year
    st.rebuild_projection(now=_NOW)
    b = st.beliefs()[0]
    assert b["status"] == "active" and abs(b["support"] - 1.0) < 1e-9   # full strength, no decay


def test_routine_decays_when_overdue_then_fades():
    # within cadence → fresh
    st = _store()
    _add(st, kind="procedural_routine", applies_when={"weekday": "MON"}, age_days=3)
    st.rebuild_projection(now=_NOW)
    assert st.beliefs()[0]["status"] == "active"
    assert abs(st.beliefs()[0]["support"] - 1.0) < 1e-9

    # overdue ~2 intervals → decayed but still standing (no contradiction added)
    st = _store()
    _add(st, kind="procedural_routine", applies_when={"weekday": "MON"}, age_days=21)
    st.rebuild_projection(now=_NOW)
    b = st.beliefs()[0]
    assert b["status"] == "active" and abs(b["support"] - 0.25) < 1e-6
    j = st.justifications_for(b["belief_id"])[0]
    assert j["raw_weight"] == 1.0 and abs(j["weight"] - 0.25) < 1e-6      # decayed weight recorded

    # long overdue → faded below the floor → dormant (forgotten, not contradicted)
    st = _store()
    _add(st, kind="procedural_routine", applies_when={"weekday": "MON"}, age_days=60)
    st.rebuild_projection(now=_NOW)
    assert st.beliefs()[0]["status"] == "dormant"


def test_silence_is_not_a_contradiction():
    """A faded routine goes dormant via lost SUPPORT weight, never by gaining negative evidence."""
    st = _store()
    _add(st, kind="procedural_routine", applies_when={"weekday": "MON"}, age_days=60)
    st.rebuild_projection(now=_NOW)
    b = st.beliefs()[0]
    assert b["contradiction"] == 0.0 and b["net"] > 0        # no negative evidence; just weak
