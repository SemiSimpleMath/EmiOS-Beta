"""Add valence / half_life_days_snapshot / extracted_by to belief_evidence.

Same class as 0001 (user_beliefs drifted behind the ORM), different table, and
missed by that migration. belief_engine/db/models.py:BeliefEvidence declares 13
columns; an existing DB has 10.

Consequence on this deployment: every UpdateBeliefsStep INSERT raised

    (sqlite3.OperationalError) table belief_evidence has no column named valence

which failed 3 of 8 domains (routine, communication, general), so the
belief_engine routine errored three runs in a row and was AUTO-DISABLED on
2026-09-12. With the engine disabled the export never ran, so
resources/kg_derived/resource_user_beliefs.json was never written and every
dayflow routine logged "Beliefs file not found" — 674 times in 24h — while
silently running with no belief context at all.

Additive and idempotent. On a fresh DB create_all builds the columns and the
runner baseline-stamps this without running it; on an existing DB it ALTERs.
All three are nullable, matching the ORM, so no backfill is needed.
"""
from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "belief_evidence"):
        return  # create_all will build it complete on first boot
    existing = {r[1] for r in conn.execute("PRAGMA table_info(belief_evidence)")}
    # (name, sqlite type) — all nullable, so a plain ADD COLUMN is safe.
    for name, coltype in (
        ("valence", "TEXT"),
        ("half_life_days_snapshot", "INTEGER"),
        ("extracted_by", "TEXT"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE belief_evidence ADD COLUMN {name} {coltype}")
