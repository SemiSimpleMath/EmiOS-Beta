"""Add completed_count / skipped_early_count to played_songs.

The DJ recorded a play at PICK time and never captured how the user reacted —
a track killed after 3 seconds and one replayed all week were identical rows.
These two columns hold the implicit-preference signal the player already emits
(MusicKit playback events), so listening can nudge the taste weights.

Additive and idempotent. On a fresh DB create_all builds the columns and the
runner baseline-stamps this without running it; on an existing DB it ALTERs.
"""
from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "played_songs"):
        return  # create_all will build it complete on first boot
    existing = {r[1] for r in conn.execute("PRAGMA table_info(played_songs)")}
    for name in ("completed_count", "skipped_early_count"):
        if name not in existing:
            conn.execute(
                f"ALTER TABLE played_songs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
