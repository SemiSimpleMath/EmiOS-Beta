"""The same observation attaches to a belief once, however often it is re-presented.

2026-09-11: collection deliberately re-reads its sources. The nightly pass re-reads a
14-day window of finished daily-insight files, and the ticket path re-tallies the same
timeline events, so the SAME observation was offered to `_insert_evidence` on every run —
which wrote a fresh row each time with no check. Measured on the live store: 343 redundant
rows across 212 groups, one insight attached ten times, contributing 30 weight where it
earns 3. Confidence is the sum of decayed evidence weights against ABSOLUTE bands (high is
net > 4.0), so re-reading was manufacturing the reconfirmation that decay exists to demand.

The guard keys on what the observation IS — (belief, source_type, source_date, summary) —
never on when it was ingested. That preserves growth and drops only repetition: a ticket
tally carries its count and date range in the summary, so a fifth snooze is different text
and lands, while an unchanged restatement does not.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

import app.assistant.tests.test_setup  # noqa: F401


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A BeliefStore pointed at a throwaway sqlite file (never emi.db or the shared test db).

    `get_database_uri` reads TEST_DATABASE_URI_EMI when USE_TEST_DB is true and
    DEV_DATABASE_URI_EMI otherwise, and another test in the suite may have set either — so
    both are pointed here. Engines are cached per URI, so a fresh URI gets a fresh engine.
    """
    uri = f"sqlite:///{str(tmp_path / 'beliefs.db').replace(chr(92), '/')}"
    monkeypatch.setenv("DEV_DATABASE_URI_EMI", uri)
    monkeypatch.setenv("TEST_DATABASE_URI_EMI", uri)

    db = tmp_path / "beliefs.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE user_beliefs (
            id TEXT PRIMARY KEY, domain TEXT, belief_key TEXT UNIQUE, statement TEXT,
            confidence TEXT, scope TEXT, status TEXT, locked INTEGER DEFAULT 0, kind TEXT,
            conditions TEXT, observation_count INTEGER DEFAULT 1, first_observed TEXT,
            last_confirmed TEXT, last_contradicted_at TEXT, current_support_weight REAL,
            current_contradiction_weight REAL, current_net_weight REAL,
            current_confidence_band TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE belief_evidence (
            id TEXT PRIMARY KEY, belief_id TEXT, source_type TEXT, source_date TEXT,
            source_ref TEXT, signal_type TEXT, summary TEXT, raw_text TEXT, weight REAL,
            valence TEXT, half_life_days_snapshot INTEGER, extracted_by TEXT, created_at TEXT);
        """
    )
    conn.execute(
        "INSERT INTO user_beliefs (id, domain, belief_key, statement, kind, status) "
        "VALUES ('b1','routine','routine.x','x','routine_pattern','active')"
    )
    conn.commit()
    conn.close()

    from belief_engine.store.belief_store import BeliefStore, EvidenceInput
    # __new__, not __init__: the constructor opens the Chroma collection, which the running
    # server holds a single-writer lock on. The evidence path under test never touches it.
    return BeliefStore.__new__(BeliefStore), EvidenceInput, db


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT source_type, source_date, summary FROM belief_evidence").fetchall()
    finally:
        conn.close()


def _ev(EvidenceInput, **kw):
    base = dict(source_type="daily_insights", source_date="2026-09-01",
                source_ref=None, signal_type="confirms",
                summary="[fact] User said: I take the dogs out at 8am.",
                raw_text=None, weight=3.0)
    base.update(kw)
    return EvidenceInput(**base)


def test_the_same_observation_attaches_once_however_often_it_is_offered(store):
    s, EvidenceInput, db = store
    ev = _ev(EvidenceInput)
    for _ in range(14):          # a fortnight of re-reading the same finished day
        s._insert_evidence("b1", ev, "2026-09-11T00:30:00+00:00")
    assert len(_rows(db)) == 1


def test_a_growing_tally_still_lands_as_new_evidence(store):
    s, EvidenceInput, db = store
    for n, days in ((3, 4), (5, 6)):
        s._insert_evidence("b1", _ev(
            EvidenceInput, source_type="ticket_rejection", signal_type="qualifies",
            summary=f"User snoozed 'hydration' without completing {n} time(s) across {days} day(s).",
        ), "2026-09-11T00:30:00+00:00")
    assert len(_rows(db)) == 2, "a changed count is a new observation and must attach"


def test_a_different_day_of_the_same_kind_of_signal_still_lands(store):
    s, EvidenceInput, db = store
    s._insert_evidence("b1", _ev(EvidenceInput, source_date="2026-09-01"), "now")
    s._insert_evidence("b1", _ev(EvidenceInput, source_date="2026-09-02"), "now")
    assert len(_rows(db)) == 2


def test_the_same_text_on_a_different_belief_is_not_blocked(store):
    s, EvidenceInput, db = store
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO user_beliefs (id, belief_key, status) VALUES ('b2','routine.y','active')")
    conn.commit(); conn.close()
    ev = _ev(EvidenceInput)
    s._insert_evidence("b1", ev, "now")
    s._insert_evidence("b2", ev, "now")
    assert len(_rows(db)) == 2


def test_rows_with_no_source_date_are_matched_null_safely(store):
    s, EvidenceInput, db = store
    ev = _ev(EvidenceInput, source_type="manual_seed", source_date=None, summary="owner note")
    s._insert_evidence("b1", ev, "now")
    s._insert_evidence("b1", ev, "now")
    assert len(_rows(db)) == 1, "NULL source_date must not defeat the guard"
