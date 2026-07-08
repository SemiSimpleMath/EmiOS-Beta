"""The archive sweep reconciles the embedding collection against the live table.

Historically only merge losers had their vectors deleted — every other deprecation path and
the archive eviction itself left ghosts behind forever. The sweep now removes every vector
whose belief id has no live user_beliefs row, healing the backlog even on nights with nothing
to archive.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from belief_engine.archive import archive_deprecated_beliefs


class _FakeChroma:
    def __init__(self, ids):
        self.ids = set(ids)
        self.deleted = []

    def all_ids(self):
        return list(self.ids)

    def delete_many(self, belief_ids):
        self.deleted.extend(belief_ids)
        self.ids -= set(belief_ids)


@pytest.fixture()
def belief_conn(monkeypatch, tmp_path):
    db = tmp_path / "beliefs.db"
    monkeypatch.setenv("USE_TEST_DB", "true")
    monkeypatch.setenv("TEST_DATABASE_URI_EMI", f"sqlite:///{db.as_posix()}")
    from app.models.base import Base, get_current_engine
    import belief_engine.db.models  # noqa: F401
    Base.metadata.create_all(get_current_engine())
    from belief_engine.db.ensure_schema import ensure_schema
    ensure_schema()
    conn = sqlite3.connect(str(db))
    yield conn
    conn.close()


def _seed(conn, belief_id, status):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO user_beliefs (id, domain, belief_key, statement, confidence, scope, status,"
        " locked, observation_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (belief_id, "routine", f"key.{belief_id}", f"statement {belief_id}", "high", "chronic",
         status, 0, 1, now, now))
    conn.commit()


def test_archive_moves_deprecated_and_removes_ghost_vectors(belief_conn):
    _seed(belief_conn, "live-1", "active")
    _seed(belief_conn, "dead-1", "deprecated")
    # Collection: both rows above, plus a long-gone ghost with no row at all.
    chroma = _FakeChroma({"live-1", "dead-1", "ancient-ghost"})

    summary = archive_deprecated_beliefs(conn=belief_conn, chroma=chroma)

    assert summary["beliefs"] == 1
    assert summary["ghost_vectors_removed"] == 2          # dead-1 (just evicted) + ancient-ghost
    assert chroma.ids == {"live-1"}
    archived = belief_conn.execute("SELECT id FROM user_beliefs_archive").fetchall()
    assert archived == [("dead-1",)]
    live = belief_conn.execute("SELECT id FROM user_beliefs").fetchall()
    assert live == [("live-1",)]


def test_reconcile_runs_even_with_nothing_to_archive(belief_conn):
    _seed(belief_conn, "live-1", "active")
    chroma = _FakeChroma({"live-1", "old-ghost-a", "old-ghost-b"})

    summary = archive_deprecated_beliefs(conn=belief_conn, chroma=chroma)

    assert summary["beliefs"] == 0
    assert summary["ghost_vectors_removed"] == 2
    assert chroma.ids == {"live-1"}
