"""dayflow_orchestrator.work_store — the dayflow WorkObject store.

Per decision #56, dayflow's work objects live in **emi.db**: the WorkStore's four tables
(work_objects / nodes / edges / events) sit alongside unified_log_2026, so the planner's portfolio
projection and the item-state lifecycle are transactional joins in one DB (no two-store coordination).
Verified: no name collision with emi.db's existing tables.

The WorkStore is single-writer by design; on the shared emi.db it coexists with the main db_manager
writer via WAL (already enabled on emi.db) + a busy_timeout so a write waits politely instead of
failing with "database is locked". Dayflow advances work objects sequentially per tick, so write
contention is low.

DAYFLOW_WORK_DB overrides the path (tests / a copy).
"""
from __future__ import annotations

import os

_BUSY_TIMEOUT_MS = 10_000
_stores: dict[str, object] = {}


def dayflow_work_db_path() -> str:
    """The path of the dayflow WorkObject store — emi.db by default, DAYFLOW_WORK_DB if set."""
    override = os.environ.get("DAYFLOW_WORK_DB")
    if override:
        return override
    from app.assistant.utils.path_utils import get_data_dir
    return str(get_data_dir() / "emi.db")


def get_dayflow_work_store():
    """The (singleton, per-path) dayflow WorkStore. Creates the four work tables IF NOT EXISTS on the
    target DB (additive — no collision with emi.db's schema) and sets a busy_timeout so it coexists with
    the main writer."""
    path = dayflow_work_db_path()
    store = _stores.get(path)
    if store is None:
        from work_objects.store import WorkStore
        store = WorkStore(path)
        # Wait for the main writer instead of erroring; harmless if the attribute name ever changes.
        try:
            conn = getattr(store, "conn", None) or getattr(store, "_conn", None)
            if conn is not None:
                conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        except Exception:
            pass
        _stores[path] = store
    return store
