"""Migration snapshots must land somewhere writable in EVERY deployment.

`backup_database()` defaulted its destination to `get_repo_root()/"backups"` — the
CODE root. That is fine in a dev checkout but wrong in a container, where the code
root is root-owned and the app runs unprivileged: the `mkdir` raised PermissionError
inside `run_migrations()`, which aborts startup by design ("a backup failure aborts
startup — fail loud"). Because `migrations/` shipped empty, no pending migration ever
existed and the crash stayed latent; the first migration to land would have taken out
every containerized install on update.

The destination is now derived from the DB's own directory, which is writable by
definition — SQLite could not have opened the db read-write otherwise.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.assistant.database.migration_backup import backup_database


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('payload')")
    conn.commit()
    conn.close()
    return path


def test_backup_lands_beside_the_db(tmp_path):
    """The regression: a DB outside the code root backs up next to itself."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _make_db(data_dir / "emi.db")

    dest = backup_database(reason="unit", db_path=db)

    assert dest is not None
    assert dest.parent == data_dir / "backups"
    assert dest.exists()


def test_backup_does_not_touch_the_code_root(tmp_path):
    """Hard guard: nothing may be written under the (possibly read-only) code root."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _make_db(data_dir / "emi.db")

    backup_database(reason="unit", db_path=db)

    assert not (REPO_ROOT / "backups" / f"emi.db.unit.bak").exists()


def test_backup_succeeds_when_code_root_is_unwritable(tmp_path, monkeypatch):
    """Simulates the container: code root cannot be written, DB dir can."""
    import app.assistant.database.migration_backup as mb

    unwritable = tmp_path / "app"
    unwritable.mkdir()
    unwritable.chmod(0o555)
    monkeypatch.setattr(mb, "get_repo_root", lambda: unwritable)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _make_db(data_dir / "emi.db")

    dest = mb.backup_database(reason="container", db_path=db)

    assert dest is not None and dest.exists()
    assert dest.parent == data_dir / "backups"
    unwritable.chmod(0o755)  # so tmp_path cleanup can proceed


def test_snapshot_is_a_readable_copy(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _make_db(data_dir / "emi.db")

    dest = backup_database(reason="unit", db_path=db)

    conn = sqlite3.connect(str(dest))
    assert conn.execute("SELECT v FROM t").fetchone() == ("payload",)
    conn.close()


def test_explicit_backups_dir_still_wins(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = _make_db(data_dir / "emi.db")
    chosen = tmp_path / "elsewhere"

    dest = backup_database(reason="unit", db_path=db, backups_dir=chosen)

    assert dest is not None and dest.parent == chosen


def test_missing_db_returns_none(tmp_path):
    assert backup_database(reason="unit", db_path=tmp_path / "nope.db") is None
