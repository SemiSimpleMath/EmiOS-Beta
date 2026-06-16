"""#3 contextual retrieval as a SOFT ranker (not a gate): the temporal cue reorders beliefs but
never excludes them; status is the only hard filter; relevance orders by context. The point is
high recall for the LLM to judge — a wrong-day belief still comes back, just lower. No model load.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from belief_engine_v2.retrieval import (
    _T_MATCH,
    _T_MISS,
    _T_NEUTRAL,
    _first_business_day,
    beliefs_for_context,
    temporal_applicability,
)
from belief_engine_v2.store import Claim, Store


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _monday(hour=9):
    d = datetime(2026, 6, 1, hour, 0)
    return d - timedelta(days=d.weekday())        # Monday of that week, guaranteed


def _add(store, subject, predicate, obj, statement, applies_when, occurred_at, *,
         polarity="affirm", strength=1.0):
    store.append(
        Claim(subject=subject, predicate=predicate, object=obj,
              statement_nl=statement, applies_when=applies_when),
        source="seed", polarity=polarity, occurred_at=occurred_at, recorded_at=occurred_at,
        strength=strength,
    )


def _objs(items):
    return [b["object"] for b in items]


# ── temporal is a SOFT score, never a gate ───────────────────────────────────
def test_temporal_applicability_is_soft_never_zero():
    mon = _monday()
    assert temporal_applicability(None, mon) == _T_NEUTRAL                 # evergreen
    assert temporal_applicability({"weekday": "MON"}, mon) == _T_MATCH      # match → boost
    assert temporal_applicability({"weekday": "TUE"}, mon) == _T_MISS       # miss → SINK, not 0
    assert temporal_applicability({"anchor": "birthday(robin)"}, mon) == _T_NEUTRAL   # can't judge
    # the load-bearing invariant: a miss sinks but stays in contention
    assert _T_MISS > 0.0 and _T_MATCH > _T_NEUTRAL > _T_MISS


def test_wrong_day_belief_is_downranked_not_excluded():
    store = _store()
    mon = _monday()
    occ = (mon - timedelta(days=1)).isoformat()
    _add(store, "alex", "does_routine", "weekly timesheet",
         "Monday is weekly timesheet day, submit by 10:00", {"weekday": "MON", "at": "10:00"}, occ)
    _add(store, "kids", "dislikes", "zucchini", "the kids dislike zucchini", None, occ)
    store.rebuild_projection()

    on_mon = _objs(beliefs_for_context(store, mon))
    on_tue = _objs(beliefs_for_context(store, mon + timedelta(days=1)))
    # NOTHING is excluded on either day (high recall — the LLM decides)
    assert set(on_mon) == set(on_tue) == {"weekly timesheet", "zucchini"}
    # but the ranking flips: timesheet floats up on Monday, sinks on Tuesday
    assert on_mon[0] == "weekly timesheet"
    assert on_tue[0] == "zucchini" and on_tue[-1] == "weekly timesheet"


def test_first_business_day_ranks_up_but_stays_when_off():
    store = _store()
    base = datetime(2026, 6, 1, 9, 0)
    fbd = base.replace(day=_first_business_day(base))
    occ = (fbd - timedelta(days=2)).isoformat()
    _add(store, "alex", "does_routine", "monthly timesheet",
         "first business day: monthly timesheet due 17:00", {"anchor": "first_business_day"}, occ)
    _add(store, "home", "sets", "cooling", "cool the house at 9pm", None, occ)
    store.rebuild_projection()

    assert _objs(beliefs_for_context(store, fbd))[0] == "monthly timesheet"      # boosted on FBD
    later = fbd + timedelta(days=9)                                              # not the FBD
    off = _objs(beliefs_for_context(store, later))
    assert "monthly timesheet" in off and off[0] == "cooling"                   # present, down-ranked


def test_status_is_the_only_hard_filter():
    store = _store()
    mon = _monday()
    occ = (mon - timedelta(days=1)).isoformat()
    _add(store, "kids", "likes", "zucchini", "kids like zucchini", None, occ, strength=1.0)
    _add(store, "kids", "likes", "zucchini", "kids like zucchini", None, occ,
         polarity="contradict", strength=2.0)                                   # net<0 → dormant
    store.rebuild_projection()
    assert beliefs_for_context(store, mon) == []                                # only hard cut


def test_daily_setpoints_surface_for_morning_planning():
    """The 6am-cooling-off / 9pm-cooling-on pair both reach the morning routine (day horizon),
    each with its slot time — time-of-day is a hint here, not scored."""
    store = _store()
    mon = _monday(hour=9)
    occ = (mon - timedelta(days=1)).isoformat()
    _add(store, "home", "sets", "cooling off",
         "6am: turn cooling off, let the house warm naturally", {"at": "06:00"}, occ)
    _add(store, "home", "sets", "cooling on", "9pm: turn the cooling on", {"at": "21:00"}, occ)
    store.rebuild_projection()
    assert set(_objs(beliefs_for_context(store, mon))) == {"cooling off", "cooling on"}


def test_instant_horizon_favors_whats_firing_now():
    store = _store()
    mon = _monday(hour=9)
    occ = (mon - timedelta(days=1)).isoformat()
    _add(store, "home", "sets", "cooling on", "9pm: turn the cooling on", {"at": "21:00"}, occ)
    _add(store, "kids", "dislikes", "zucchini", "the kids dislike zucchini", None, occ)
    store.rebuild_projection()

    at9 = _objs(beliefs_for_context(store, mon.replace(hour=9), horizon="instant"))
    at21 = _objs(beliefs_for_context(store, mon.replace(hour=21), horizon="instant"))
    assert set(at9) == set(at21) == {"cooling on", "zucchini"}                  # never excluded
    assert at9[-1] == "cooling on"                                              # not firing at 09:00
    assert at21[0] == "cooling on"                                              # fires at 21:00


def test_relevance_orders_by_context():
    def emb(text: str):
        return [1.0, 0.0] if ("zucchini" in text.lower() or "caffeine" in text.lower()) else [0.0, 1.0]

    store = _store()
    mon = _monday()
    occ = (mon - timedelta(days=1)).isoformat()
    _add(store, "kids", "dislikes", "zucchini", "the kids dislike zucchini", None, occ)
    _add(store, "home", "sets", "cooling", "cool the house at 9pm", None, occ)
    store.rebuild_projection()
    ranked = beliefs_for_context(store, mon, query="zucchini for dinner", embedder=emb)
    assert ranked[0]["object"] == "zucchini"


def test_k_limits_results():
    store = _store()
    mon = _monday()
    occ = (mon - timedelta(days=1)).isoformat()
    for i in range(5):
        _add(store, "x", "p", f"obj{i}", f"statement {i}", None, occ)
    store.rebuild_projection()
    assert len(beliefs_for_context(store, mon, k=3)) == 3


def test_usage_boosts_a_surfaced_belief():
    """A belief that's been SURFACED to a consumer outranks an otherwise-identical belief that
    hasn't — importance earned from USE (surfacing_log). With no surfacing both score 0 on usage,
    so the term is a pure no-op until real use accrues (zero regression for the other tests)."""
    from belief_engine_v2.surfacing import log_surfaced
    store = _store()
    mon = _monday()
    occ = (mon - timedelta(days=1)).isoformat()
    # identical base signals (same cue, recency, obs count) -> only usage can separate them
    _add(store, "kids", "likes", "apples", "the kids like apples", None, occ)
    _add(store, "kids", "likes", "pears", "the kids like pears", None, occ)
    store.rebuild_projection()

    base = beliefs_for_context(store, mon, include_scores=True)
    assert {b["object"] for b in base} == {"apples", "pears"}
    assert all(b["_scores"]["usage"] == 0.0 for b in base)      # no surfacing yet -> term is 0

    apples_id = next(b["belief_id"] for b in base if b["object"] == "apples")
    for _ in range(5):
        log_surfaced(store.conn, agent="test_consumer", rows=[{"belief_id": apples_id, "score": 1.0}])

    ranked = beliefs_for_context(store, mon, include_scores=True)
    assert ranked[0]["object"] == "apples"                      # usage boost lifts it to the top
    assert next(b for b in ranked if b["object"] == "apples")["_scores"]["usage"] > 0.0
    assert next(b for b in ranked if b["object"] == "pears")["_scores"]["usage"] == 0.0
