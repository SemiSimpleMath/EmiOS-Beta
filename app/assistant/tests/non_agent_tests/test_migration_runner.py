"""Tests for app/database/migration_runner.py — the schema-migration runner.

Locks in the four behaviours auto-update safety depends on:
  1. FRESH db        -> migrations are baseline-stamped, NOT run (create_all already
                        built the latest schema; running historical ALTERs would error).
  2. EXISTING old db -> pending migrations actually run (the whole point).
  3. RE-RUN          -> no-op, idempotent, no duplicate schema_migrations rows.
  4. NEW migration on an already-baselined db -> only the new one runs.

Uses synthetic migrations written into a temp dir so the runner logic is tested in
isolation from the real schema.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database.migration_runner import run_migrations


# A migration that (a) idempotently adds a column and (b) drops a detectable marker
# so a test can tell whether up() actually executed.
_MIG_0001 = """
def up(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(widgets)")]
    if "extra" not in cols:
        conn.execute("ALTER TABLE widgets ADD COLUMN extra TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS mig_marker (mid TEXT)")
    conn.execute("INSERT INTO mig_marker (mid) VALUES ('0001')")
"""

_MIG_0002 = """
def up(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(widgets)")]
    if "extra2" not in cols:
        conn.execute("ALTER TABLE widgets ADD COLUMN extra2 TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS mig_marker (mid TEXT)")
    conn.execute("INSERT INTO mig_marker (mid) VALUES ('0002')")
"""


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


def _cols(db: str, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _markers(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        if not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mig_marker'"
        ).fetchone():
            return []
        return [r[0] for r in con.execute("SELECT mid FROM mig_marker ORDER BY mid")]
    finally:
        con.close()


def _recorded(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT id FROM schema_migrations ORDER BY id")]
    finally:
        con.close()


def test_fresh_db_baselines_without_running(tmp_path):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "fresh.db")
    # create_all already built the LATEST schema (extra present).
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, extra TEXT)")
    con.commit(); con.close()

    recorded = run_migrations(db, fresh_db=True, migrations_dir=migs)

    assert recorded == ["0001_add_extra"]      # stamped
    assert _recorded(db) == ["0001_add_extra"]
    assert _markers(db) == []                   # up() did NOT run


def test_existing_old_db_applies_migration(tmp_path):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "old.db")
    # Old schema: widgets WITHOUT the `extra` column.
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
    con.commit(); con.close()

    recorded = run_migrations(db, fresh_db=False, migrations_dir=migs)

    assert recorded == ["0001_add_extra"]
    assert "extra" in _cols(db, "widgets")      # up() ran -> column added
    assert _markers(db) == ["0001"]             # up() ran -> marker


def test_rerun_is_idempotent_noop(tmp_path):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "rerun.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
    con.commit(); con.close()

    run_migrations(db, fresh_db=False, migrations_dir=migs)
    second = run_migrations(db, fresh_db=False, migrations_dir=migs)

    assert second == []                          # nothing new applied
    assert _recorded(db) == ["0001_add_extra"]   # no duplicate row
    assert _markers(db) == ["0001"]              # up() not re-run


def test_new_migration_on_baselined_db_runs(tmp_path):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "evolve.db")
    # Fresh db built at the schema known at baseline time (extra present, extra2 not).
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, extra TEXT)")
    con.commit(); con.close()
    run_migrations(db, fresh_db=True, migrations_dir=migs)      # baseline 0001

    # A NEW migration ships later; db is no longer fresh.
    _write(migs, "0002_add_extra2.py", _MIG_0002)
    recorded = run_migrations(db, fresh_db=False, migrations_dir=migs)

    assert recorded == ["0002_add_extra2"]                     # only the new one
    assert "extra2" in _cols(db, "widgets")
    assert _markers(db) == ["0002"]                            # 0001 baselined (never ran)
    assert _recorded(db) == ["0001_add_extra", "0002_add_extra2"]


# ── backup-before-migration (R5): snapshot only when a real ALTER hits an existing db ──
def _patch_backup(monkeypatch):
    """Record backup_database calls without touching the real emi.db / backups dir."""
    import app.assistant.database.migration_backup as mb
    calls = []
    monkeypatch.setattr(mb, "backup_database", lambda *a, **k: calls.append(k.get("reason", "?")))
    return calls


def test_backup_runs_before_applying_to_existing_db(tmp_path, monkeypatch):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")    # old schema, missing `extra`
    con.commit(); con.close()
    calls = _patch_backup(monkeypatch)

    run_migrations(db, fresh_db=False, migrations_dir=migs)

    assert len(calls) == 1                                          # snapshotted once, before the ALTER
    assert "extra" in _cols(db, "widgets")                          # and the migration still applied


def test_no_backup_on_fresh_db(tmp_path, monkeypatch):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "fresh.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, extra TEXT)")
    con.commit(); con.close()
    calls = _patch_backup(monkeypatch)

    run_migrations(db, fresh_db=True, migrations_dir=migs)          # baseline-stamp only

    assert calls == []                                             # nothing to lose -> no backup


def test_no_backup_when_up_to_date(tmp_path, monkeypatch):
    migs = tmp_path / "migrations"; migs.mkdir()
    _write(migs, "0001_add_extra.py", _MIG_0001)
    db = str(tmp_path / "current.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
    con.commit(); con.close()
    calls = _patch_backup(monkeypatch)   # patch BEFORE any run so the real backups dir is never touched

    run_migrations(db, fresh_db=False, migrations_dir=migs)         # applies 0001 -> one backup
    assert len(calls) == 1
    calls.clear()

    second = run_migrations(db, fresh_db=False, migrations_dir=migs)  # nothing pending now
    assert second == [] and calls == []                            # no pending -> no backup
