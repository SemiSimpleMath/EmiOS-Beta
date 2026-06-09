"""Reliability R5: backup-before-migration hook.

Covers backup_database (creates a valid restorable snapshot; None when there's no DB; prunes to the
last N) and run_with_backup (backs up THEN migrates; aborts the migration if the backup fails — never
migrate a DB we can't roll back). Uses temp paths so the real emi.db / backups/ are never touched.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.assistant.database import migration_backup as mb


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()


def test_snapshot_created_and_restorable(tmp_path):
    db = tmp_path / "emi.db"
    _make_db(db)
    dest = mb.backup_database("unit", db_path=db, backups_dir=tmp_path / "backups")
    assert dest is not None and dest.exists()
    # The snapshot is a real, queryable copy of the data.
    c = sqlite3.connect(str(dest))
    try:
        assert c.execute("SELECT x FROM t").fetchone()[0] == 42
    finally:
        c.close()


def test_none_when_no_db(tmp_path):
    assert mb.backup_database("x", db_path=tmp_path / "nope.db", backups_dir=tmp_path / "b") is None


def test_prune_keeps_last_n(tmp_path):
    db = tmp_path / "emi.db"
    _make_db(db)
    backups = tmp_path / "backups"
    for i in range(5):
        mb.backup_database(f"r{i}", db_path=db, backups_dir=backups, keep=2)
    assert len(list(backups.glob("emi.db.*.bak"))) == 2


def test_run_with_backup_backs_up_then_migrates(monkeypatch):
    calls = []
    monkeypatch.setattr(mb, "backup_database", lambda reason="m", **k: calls.append("backup"))
    out = mb.run_with_backup(lambda: (calls.append("migrate"), "done")[1], name="x")
    assert calls == ["backup", "migrate"]
    assert out == "done"


def test_run_with_backup_aborts_if_backup_fails(monkeypatch):
    def _boom(reason="m", **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(mb, "backup_database", _boom)
    ran = []
    with pytest.raises(RuntimeError):
        mb.run_with_backup(lambda: ran.append("migrate"), name="x")
    assert ran == []   # the migration never ran without a backup
