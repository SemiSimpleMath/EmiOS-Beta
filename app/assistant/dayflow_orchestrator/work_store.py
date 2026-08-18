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
import threading

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_BUSY_TIMEOUT_MS = 10_000
_stores: dict[str, object] = {}
_stores_lock = threading.Lock()


def _migrate_node_active_to_dispatched(conn) -> None:
    """One-time, idempotent: rename spine/notify node status 'active' -> 'dispatched' (the in-flight marker).
    A no-op once migrated. Verification nodes keep 'active' (a verification run in progress)."""
    try:
        with conn:
            cur = conn.execute(
                "UPDATE nodes SET status='dispatched' WHERE status='active' AND type != 'verification'")
        n = cur.rowcount or 0
        if n > 0:
            logger.info("[work_store] migrated %d node(s) status 'active' -> 'dispatched'", n)
    except Exception as e:
        logger.error("[work_store] active->dispatched migration failed: %s", e)


def _migrate_parked_asks_to_dispatched(conn) -> None:
    """One-time (2026-08-18 owner ruling): a surfaced ask is an IN-FLIGHT tool call and its
    status must say so. Old rows parked surfaced asks as 'waiting' + wake_kind=user_reply with
    a re-ask timer; the re-ask timer is retired — flip them to 'dispatched' and clear wake_at
    so nothing re-promotes them. Their tickets resolve them (reply -> done) or the sweeper
    times them out (expired ticket -> failed, work_repair adjudicates).

    Guarded by a marker (work_store_meta) because it must run EXACTLY once: under the new
    model 'waiting' + user_reply legitimately reappears for PRE-surface asks parked by a
    state_mover HOLD — a re-run would mis-flip those to in-flight."""
    _KEY = "parked_asks_to_dispatched_2026_08_18"
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS work_store_meta (key TEXT PRIMARY KEY, value TEXT)")
            done = conn.execute("SELECT value FROM work_store_meta WHERE key=?", (_KEY,)).fetchone()
            if done is not None:
                return
            cur = conn.execute(
                "UPDATE nodes SET status='dispatched', wake_at=NULL "
                "WHERE status='waiting' AND wake_kind='user_reply'")
            conn.execute("INSERT INTO work_store_meta(key, value) VALUES(?, ?)",
                         (_KEY, str(cur.rowcount or 0)))
        n = cur.rowcount or 0
        if n > 0:
            logger.info("[work_store] migrated %d parked ask(s) 'waiting' -> 'dispatched'", n)
    except Exception as e:
        logger.error("[work_store] parked-ask migration failed: %s", e)


def dayflow_work_db_path() -> str:
    """The path of the dayflow WorkObject store — emi.db by default, DAYFLOW_WORK_DB if set."""
    override = os.environ.get("DAYFLOW_WORK_DB")
    if override:
        return override
    from app.assistant.utils.path_utils import get_data_dir
    return str(get_data_dir() / "emi.db")


def get_dayflow_work_store():
    """The (singleton, per-path) dayflow WorkStore. Creates the four work tables IF NOT EXISTS on the
    target DB (additive — no collision with emi.db's schema); the busy_timeout rides the WorkStore
    constructor so it coexists with the main writer.

    Locked double-checked creation: two threads racing the first touch used to
    build two WorkStore instances with two separate RLocks — breaking the
    "one lock serializes all writers" guarantee for the overlap window
    (audit W3)."""
    path = dayflow_work_db_path()
    store = _stores.get(path)
    if store is not None:
        return store
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            from work_objects.store import WorkStore
            store = WorkStore(path, busy_timeout_ms=_BUSY_TIMEOUT_MS)
            # Same-package private access, on purpose: the migrations run on the
            # store's own connection and log their own failures loudly.
            _migrate_node_active_to_dispatched(store._conn)
            _migrate_parked_asks_to_dispatched(store._conn)
            # Closure-cascade repair (2026-07-30 zombie-wake incident): terminal work
            # objects created before closure cascaded may still hold startable nodes
            # with armed wakes. Must run before any apply() touches those rows — the
            # closure invariant in WorkObject.validate() rejects them otherwise.
            # Logged HERE: work_objects/ is app-independent and its stdlib logger
            # does not reach the app's log files.
            repaired = store.repair_terminal_zombies()
            if repaired:
                logger.warning(
                    "[work_store] closure-cascade repair: abandoned %d startable node(s) "
                    "inside terminal work objects (a nonzero count after the first run "
                    "means a writer bypassed the closure invariant)", repaired)
            _stores[path] = store
    return store
