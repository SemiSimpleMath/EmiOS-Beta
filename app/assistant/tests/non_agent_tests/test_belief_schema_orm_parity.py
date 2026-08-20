"""`belief_engine/db/schema.py` must stay in lockstep with `belief_engine/db/models.py`.

`user_beliefs` is not registered on `Base.metadata` (`_register_all_models()` does not
import the belief models), so `create_all` never builds it — `SCHEMA_SQL` is its only
creator. Nothing structural forces the two definitions to agree, and they silently
drifted six columns apart, which took out RecomputeBeliefSnapshotStep on every domain.

These tests are that missing forcing function:
  1. every ORM column/index exists in a DB built from SCHEMA_SQL (fresh install), and
  2. migration 0001 lifts an old, pre-drift DB to the same shape (upgrade install).
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.base import Base
import belief_engine.db.models  # noqa: F401  (registers UserBelief on Base.metadata)
from belief_engine.db.schema import SCHEMA_SQL

TABLE = "user_beliefs"
MIGRATION = REPO_ROOT / "migrations" / "0001_belief_schema_drift.py"


def _orm_table():
    return Base.metadata.tables[TABLE]


def _cols(conn, table=TABLE):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table=TABLE):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND name NOT LIKE 'sqlite_%'",
            (table,),
        )
    }


@pytest.fixture
def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    yield conn
    conn.close()


def test_schema_sql_has_every_orm_column(fresh_db):
    """A fresh install must be able to load a UserBelief without 'no such column'."""
    missing = {c.name for c in _orm_table().c} - _cols(fresh_db)
    assert not missing, f"SCHEMA_SQL is missing ORM columns: {sorted(missing)}"


def test_schema_sql_has_every_orm_index(fresh_db):
    missing = {i.name for i in _orm_table().indexes} - _indexes(fresh_db)
    assert not missing, f"SCHEMA_SQL is missing ORM indexes: {sorted(missing)}"


def test_full_orm_column_select_succeeds(fresh_db):
    """Regression: the ORM emits all columns, so any gap breaks every belief load."""
    names = ", ".join(c.name for c in _orm_table().c)
    fresh_db.execute(f"SELECT {names} FROM {TABLE}").fetchall()


def test_decay_recompute_query_succeeds(fresh_db):
    """Regression for the exact raw SQL in belief_engine/decay/recompute.py."""
    fresh_db.execute(
        "SELECT id, domain, belief_key, kind, status, scope FROM user_beliefs "
        "WHERE status = 'active' AND domain = ?",
        ("routine",),
    ).fetchall()


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0001", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_drift_db():
    """A DB at the schema that shipped before the drifted columns were added."""
    drifted = [
        "kind",
        "last_contradicted_at",
        "current_support_weight",
        "current_contradiction_weight",
        "current_net_weight",
        "current_confidence_band",
    ]
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    keep = [c for c in _cols(conn) if c not in drifted]
    conn.execute(f"CREATE TABLE ub_old AS SELECT {', '.join(keep)} FROM {TABLE} WHERE 0")
    conn.execute(f"DROP TABLE {TABLE}")
    conn.execute(f"ALTER TABLE ub_old RENAME TO {TABLE}")
    return conn


def test_migration_lifts_old_db_to_orm_shape():
    conn = _pre_drift_db()
    assert "kind" not in _cols(conn), "fixture failed to reproduce the pre-drift schema"
    _load_migration().up(conn)
    missing = {c.name for c in _orm_table().c} - _cols(conn)
    assert not missing, f"migration left columns missing: {sorted(missing)}"
    conn.close()


def test_migration_is_idempotent():
    conn = _pre_drift_db()
    module = _load_migration()
    module.up(conn)
    once = _cols(conn)
    module.up(conn)
    module.up(conn)
    assert _cols(conn) == once
    conn.close()


def test_migration_preserves_existing_rows():
    conn = _pre_drift_db()
    conn.execute(
        f"INSERT INTO {TABLE} (id, domain, belief_key, statement, confidence, scope, "
        "status, locked, observation_count, created_at, updated_at) "
        "VALUES ('u1','routine','r.k','s','high','chronic','active',0,1,'t','t')"
    )
    _load_migration().up(conn)
    assert conn.execute(f"SELECT id, kind FROM {TABLE}").fetchall() == [("u1", None)]
    conn.close()


def test_migration_noops_when_table_absent():
    """An older app DB may predate the belief engine; ensure_schema creates it after."""
    conn = sqlite3.connect(":memory:")
    _load_migration().up(conn)  # must not raise
    conn.close()
