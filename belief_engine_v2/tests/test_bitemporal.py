"""#7 bitemporal: transaction-time travel (what the engine believed as of a system time, by
replaying the immutable log up to it — no version table) + world-time validity (valid_from/to).
No model load."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

from belief_engine_v2.store import Claim, Store

_NOW = datetime(2026, 6, 1, 12, 0)


def _store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _add(st, *, polarity="affirm", strength=1.0, t):
    return st.append(
        Claim(subject="kids", predicate="eat", object="zucchini", statement_nl="kids eat zucchini"),
        source="daily_insight" if polarity == "affirm" else "user_comment",
        polarity=polarity, occurred_at=t, recorded_at=t, strength=strength)


def _seed_flip(st):
    _add(st, t="2026-01-01T09:00:00")                                  # affirmed in January
    _add(st, polarity="contradict", strength=2.0, t="2026-03-01T09:00:00")  # contradicted in March
    st.rebuild_projection(now=_NOW)


def test_transaction_time_travel():
    st = _store(); _seed_flip(st)
    assert st.beliefs()[0]["status"] == "dormant"                       # live: both observations

    feb = st.beliefs_as_of("2026-02-01T00:00:00", now=_NOW)
    assert feb[0]["status"] == "active"                                 # March contradiction not yet known
    apr = st.beliefs_as_of("2026-04-01T00:00:00", now=_NOW)
    assert apr[0]["status"] == "dormant"                               # now it's in scope


def test_as_of_does_not_clobber_live_projection():
    st = _store(); _seed_flip(st)
    assert st.beliefs()[0]["status"] == "dormant"
    _ = st.beliefs_as_of("2026-02-01T00:00:00", now=_NOW)              # a time-travel read
    assert st.beliefs()[0]["status"] == "dormant"                     # live projection untouched


def test_valid_from_and_to():
    st = _store()
    _add(st, t="2026-01-01T09:00:00")
    st.rebuild_projection(now=_NOW)
    b = st.beliefs()[0]
    assert b["valid_from"] == "2026-01-01T09:00:00" and b["valid_to"] is None   # still holds

    _add(st, polarity="contradict", strength=2.0, t="2026-03-01T09:00:00")
    st.rebuild_projection(now=_NOW)
    b = st.beliefs()[0]
    assert b["status"] == "dormant" and b["valid_to"] == "2026-03-01T09:00:00"  # lapsed when contradicted
