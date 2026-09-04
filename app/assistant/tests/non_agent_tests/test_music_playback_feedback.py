"""Implicit taste from playback reactions (2026-09-04).

The DJ recorded a play at pick time and threw away how the user reacted. Now a
completion nudges the song's weight up, an early skip nudges it down — bounded,
floored, and never lifting an explicit ban. The classification logic and the
bounded nudge are what need pinning; both run against a tiny temp DB so the
weight upserts are exercised for real without touching emi.db.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from app.assistant.dj_manager import playback_feedback as pf

# CRITICAL (learned the hard way, twice): this module's get_session() must NOT
# resolve to the real emi.db. A first pass of this test overwrote the user's
# actual Pink Floyd taste weight (2.0 -> 1.075). So the fixture owns its own
# temp SQLite file and monkeypatches get_session in the module under test to
# hand out connections to THAT file — nothing here can reach emi.db regardless
# of how session routing is configured.


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "feedback_test.db"

    class _Sess:
        def __init__(self, conn): self._c = conn
        def execute(self, clause, params=None):
            sql = getattr(clause, "text", str(clause))
            return _Rows(self._c.execute(sql, params or {}))
        def commit(self): self._c.commit()
        def rollback(self): self._c.rollback()
        def close(self): pass

    class _Rows:
        def __init__(self, cur): self._cur = cur
        def fetchone(self): return self._cur.fetchone()

    conn = sqlite3.connect(str(dbfile), check_same_thread=False)
    conn.executescript(
        "CREATE TABLE played_songs (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "first_played_utc TEXT, last_played_utc TEXT, "
        "completed_count INTEGER DEFAULT 0, skipped_early_count INTEGER DEFAULT 0);"
        "CREATE TABLE music_track_weights (track_key TEXT PRIMARY KEY, title TEXT, "
        "artist TEXT, factor REAL, updated_at_utc TEXT);"
        "CREATE TABLE music_artist_weights (artist TEXT PRIMARY KEY, factor REAL, "
        "updated_at_utc TEXT);"
    )
    conn.commit()
    monkeypatch.setattr(pf, "get_session", lambda: _Sess(conn))
    yield conn
    conn.close()


def _track_factor(conn, track_id):
    r = conn.execute("SELECT factor FROM music_track_weights WHERE track_key=?",
                     (f"spotify:{track_id}",)).fetchone()
    return float(r[0]) if r else None


def test_completion_classifies_and_nudges_up(_isolated_db):
    r = pf.record_reaction(title="Time", artist="Pink Floyd", track_id="t1",
                           played_seconds=380, duration_seconds=400)
    assert r == "completed"
    assert _track_factor(_isolated_db, "t1") == pytest.approx(1.0 + pf.NUDGE)


def test_early_skip_classifies_and_nudges_down(_isolated_db):
    r = pf.record_reaction(title="Blip", artist="Nobody", track_id="t2",
                           played_seconds=4, duration_seconds=200)
    assert r == "skipped_early"
    assert _track_factor(_isolated_db, "t2") == pytest.approx(1.0 - pf.NUDGE)


def test_mid_track_skip_is_neutral_no_write(_isolated_db):
    r = pf.record_reaction(title="Mid", artist="Nobody", track_id="t3",
                           played_seconds=120, duration_seconds=300)
    assert r == "neutral"
    assert _track_factor(_isolated_db, "t3") is None, "a mid-track skip carries no signal"


def test_nudge_is_floored(_isolated_db):
    _isolated_db.execute("INSERT INTO music_track_weights (track_key, factor, updated_at_utc) "
                         "VALUES ('spotify:t4', ?, '2026-01-01')", (pf.MIN_FACTOR,))
    _isolated_db.commit()
    pf.record_reaction(title="x", artist="y", track_id="t4",
                       played_seconds=2, duration_seconds=200)
    assert _track_factor(_isolated_db, "t4") == pytest.approx(pf.MIN_FACTOR), "cannot skip below the floor"


def test_completion_never_resurrects_a_ban(_isolated_db):
    _isolated_db.execute("INSERT INTO music_track_weights (track_key, factor, updated_at_utc) "
                         "VALUES ('spotify:t5', 0.0, '2026-01-01')")
    _isolated_db.commit()
    pf.record_reaction(title="x", artist="y", track_id="t5",
                       played_seconds=390, duration_seconds=400)
    assert _track_factor(_isolated_db, "t5") == pytest.approx(0.0), "an explicit ban outranks a completion"


def test_reaction_counter_increments_on_played_songs(_isolated_db):
    _isolated_db.execute("INSERT INTO played_songs (title, artist, first_played_utc, last_played_utc) "
                         "VALUES ('Time','Pink Floyd','2026-01-01','2026-01-01')")
    _isolated_db.commit()
    pf.record_reaction(title="Time", artist="Pink Floyd", track_id="t1",
                       played_seconds=390, duration_seconds=400)
    n = _isolated_db.execute("SELECT completed_count FROM played_songs WHERE title='Time'").fetchone()[0]
    assert n == 1
