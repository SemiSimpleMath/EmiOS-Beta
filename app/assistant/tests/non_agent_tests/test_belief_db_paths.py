"""belief_db_path is the single path authority — test-DB routing reaches the raw-sqlite writers.

identity.py (short ids, merge provenance) used to hardcode repo-root emi.db, so any test that
upserted a belief planted orphan rows in the PRODUCTION belief_short_id table. All raw-sqlite
belief modules now resolve through belief_engine.db.paths.belief_db_path, which honors
USE_TEST_DB / TEST_DATABASE_URI_EMI exactly like the app's engine.
"""
from __future__ import annotations

import sqlite3

from belief_engine.db.paths import belief_db_path


def _route_to(monkeypatch, tmp_path):
    db = tmp_path / "belief_paths_test.db"
    monkeypatch.setenv("USE_TEST_DB", "true")
    monkeypatch.setenv("TEST_DATABASE_URI_EMI", f"sqlite:///{db.as_posix()}")
    return db


def test_belief_db_path_honors_test_db_env(monkeypatch, tmp_path):
    db = _route_to(monkeypatch, tmp_path)
    assert belief_db_path() == db.as_posix()


def test_identity_writes_land_in_the_routed_db(monkeypatch, tmp_path):
    db = _route_to(monkeypatch, tmp_path)

    from belief_engine.identity import ensure_short_id
    sid = ensure_short_id("belief-test-123")
    assert sid == "b1"

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT short_id FROM belief_short_id WHERE belief_id='belief-test-123'").fetchone()
        assert row == (1,)
    finally:
        conn.close()
