"""The subconscious's connection to emi.db: writes hold db_manager's writer slot for one short
transaction, reads take a read session. Rows come back as sqlite3.Row.

Used by the brain inbox (brain_events) and the concern store (concerns). Tests pass a scratch
connection provider with the same shape (belief_engine.intake.store.sqlite_file).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def connect(write: bool) -> Iterator[sqlite3.Connection]:
    from app.models.db_manager import get_db_manager
    mgr = get_db_manager()
    with (mgr.transaction(op="subconscious") if write else mgr.read_session()) as session:
        raw = session.connection().connection.driver_connection
        previous = raw.row_factory
        raw.row_factory = sqlite3.Row
        try:
            yield raw
        finally:
            raw.row_factory = previous
