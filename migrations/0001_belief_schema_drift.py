"""Add the six `user_beliefs` columns (and two indexes) that the ORM declares but
`belief_engine/db/schema.py` never created.

Background
----------
`user_beliefs` is NOT registered on `Base.metadata` — `_register_all_models()` does
not import `belief_engine.db.models`, so `create_all` never builds this table.
Its only creator is `SCHEMA_SQL` in `belief_engine/db/schema.py`, which had drifted
six columns behind `belief_engine/db/models.py`:

    kind, last_contradicted_at, current_support_weight,
    current_contradiction_weight, current_net_weight, current_confidence_band

Any ORM load of `UserBelief` emits all twenty columns, so it died with
``no such column: user_beliefs.last_contradicted_at``; the raw-SQL decay query in
``belief_engine/decay/recompute.py`` died earlier still on ``kind``. That took out
``RecomputeBeliefSnapshotStep`` on every domain, every night.

The companion fix to `schema.py` repairs *fresh* installs. This migration repairs
*existing* ones — on a fresh DB the runner baseline-stamps it without running it,
which is correct, because `schema.py` will already have built the table complete.

`kind` is intentionally left NULL here. `belief_engine/decay/model.py::classify_kind`
exists to backfill it heuristically, but choosing decay semantics for beliefs already
on disk is a product decision, not a schema one — see the PR discussion.
"""
from __future__ import annotations

import sqlite3

# column name -> SQLite type. All nullable: SQLite cannot ADD a NOT NULL column
# without a constant default, and the ORM declares every one of these nullable.
_COLUMNS = {
    "kind": "TEXT",
    "last_contradicted_at": "TEXT",
    "current_support_weight": "REAL",
    "current_contradiction_weight": "REAL",
    "current_net_weight": "REAL",
    "current_confidence_band": "TEXT",
}

_INDEXES = (
    ("ix_user_beliefs_kind", "kind"),
    ("ix_user_beliefs_current_band", "current_confidence_band"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def up(conn: sqlite3.Connection) -> None:
    # An older app DB may predate the belief engine entirely. ensure_schema() runs
    # after this migration and will create the table complete, so there is nothing
    # to alter here — bail rather than fail.
    if not _table_exists(conn, "user_beliefs"):
        return

    existing = {r[1] for r in conn.execute("PRAGMA table_info(user_beliefs)")}
    for name, coltype in _COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE user_beliefs ADD COLUMN {name} {coltype}")

    for index_name, column in _INDEXES:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON user_beliefs({column})"
        )
