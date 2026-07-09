"""
DB Manager — single point of write coordination for the application.

SQLite has a single-writer model at the page level. Application-side, with
~30 threads (Flask, dayflow, routines, KG pipeline workers, etc.) each
trying to BEGIN a write transaction independently, they collide constantly
— SQLite's busy_timeout helps them wait politely but eventually times out
with "database is locked" errors that cascade through everything.

This manager moves the serialization up to the application layer.

## API

Simple writes — call directly, blocks until commit:
    db_manager.write(obj)            # session.add(obj); commit
    db_manager.write_many(objs)      # add all + commit (one transaction)
    db_manager.delete(obj)           # session.delete(obj); commit

Read-only queries — no lock acquired, concurrent reads fine under WAL:
    with db_manager.read_session() as session:
        rows = session.query(...).all()

Multi-statement atomic transactions — when you need a session for queries
+ writes that must commit together:
    with db_manager.transaction(op="...") as session:
        session.add(thing_a)
        found = session.query(...).first()
        session.add(thing_b)
        # commits at end of with block; rolls back on exception

## Implementation

A single ``threading.Lock()`` serializes all writers — that lock IS the
queue. Each caller's thread acquires, does the work, releases. No worker
thread, no Future objects, no async submission. Same blocking semantics
the user wanted, simpler internals.

## Discipline (enforced by audit + watchdog, not at the API level)

- Don't hold the writer slot through long operations (LLM calls, HTTP
  requests, heavy scans). Pattern: open → write → commit → close, in
  milliseconds, not seconds.
- For "write provisionally → do LLM → commit-or-rollback based on
  result", split into separate transactions: write a 'pending' row, do
  the LLM call WITHOUT a session, then a second transaction either
  finalizes or deletes the pending row. The provisional state lives in
  the data, not in the transaction.

## Adoption (updated 2026-07-09)

Every hot writer rides this manager: unified_log persistence, dayflow
item writes, KG pipeline workers, llm_call_log telemetry, routine and
ticket flows. The 2026-07-09 persistence audit verified the
session-held-across-LLM pattern extinct repo-wide; the remaining raw
``get_session()`` writers are short transactions (one-off setup scripts
and a shrinking set of small stores) converted opportunistically as
their files are touched.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


# Threshold for long-write warning. If a writer holds the slot longer than
# this, log a warning. Helps catch accidental "session-over-LLM" bugs.
LONG_WRITE_THRESHOLD_S = 5.0

# Threshold for long-wait warning. If a caller waits longer than this for
# the writer slot, log it — indicates pile-up.
LONG_WAIT_THRESHOLD_S = 1.0


class DBManager:
    """Single point of write coordination.

    Thread-safe. Use the module-level ``get_db_manager()`` to access the
    singleton.
    """

    def __init__(self) -> None:
        self._writer_lock = threading.Lock()
        # Stats — incremented under the lock for write counters.
        self._writes_attempted = 0
        self._writes_succeeded = 0
        self._writes_rolled_back = 0
        self._wait_total_s = 0.0
        self._hold_total_s = 0.0
        self._stats_lock = threading.Lock()

    @contextmanager
    def transaction(self, *, op: str = "unknown") -> Iterator:
        """Acquire the global writer slot, yield a session, commit on
        clean exit or rollback on exception. Releases the slot on exit.

        Use this when you need a session for multi-statement atomic work
        (queries + writes that must commit together). For single-object
        writes, prefer the ``write()``/``write_many()``/``delete()``
        convenience methods.

        ``op`` is a short string used in long-hold warnings — pass the
        name of the operation (e.g. ``"resolver.batch"``).

        DO NOT make LLM calls or other long operations inside this
        context. Hold time should be milliseconds, not seconds.
        """
        wait_start = time.monotonic()
        with self._writer_lock:
            wait_elapsed = time.monotonic() - wait_start
            if wait_elapsed > LONG_WAIT_THRESHOLD_S:
                logger.warning(
                    "[db_manager] %s: waited %.1fs for writer slot",
                    op, wait_elapsed,
                )

            work_start = time.monotonic()
            with self._stats_lock:
                self._writes_attempted += 1
                self._wait_total_s += wait_elapsed

            session = get_session()
            committed = False
            try:
                yield session
                session.commit()
                committed = True
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                raise
            finally:
                session.close()
                work_elapsed = time.monotonic() - work_start
                with self._stats_lock:
                    self._hold_total_s += work_elapsed
                    if committed:
                        self._writes_succeeded += 1
                    else:
                        self._writes_rolled_back += 1
                if work_elapsed > LONG_WRITE_THRESHOLD_S:
                    logger.warning(
                        "[db_manager] %s: held writer slot for %.1fs "
                        "(threshold %.1fs) — this should not include LLM/HTTP work",
                        op, work_elapsed, LONG_WRITE_THRESHOLD_S,
                    )

    @contextmanager
    def read_session(self) -> Iterator:
        """Read-only session. No lock acquired — many concurrent readers
        fine under WAL.

        Don't use for writes. If you do, you'll get the same lock
        contention this manager is trying to fix.
        """
        session = get_session()
        try:
            yield session
        finally:
            session.close()

    # --------------------------------------------------------------
    # Convenience methods — the common case shouldn't need a `with` block.
    # --------------------------------------------------------------

    def write(self, obj, *, op: str = "write") -> None:
        """Insert one ORM object. Blocks until commit. Errors propagate."""
        with self.transaction(op=op) as session:
            session.add(obj)

    def write_many(self, objs, *, op: str = "write_many") -> int:
        """Insert N ORM objects in one transaction. Returns count written."""
        objs = list(objs)
        if not objs:
            return 0
        with self.transaction(op=op) as session:
            for obj in objs:
                session.add(obj)
        return len(objs)

    def delete(self, obj, *, op: str = "delete") -> None:
        """Delete one ORM object. Blocks until commit."""
        with self.transaction(op=op) as session:
            session.delete(obj)

    def merge(self, obj, *, op: str = "merge") -> None:
        """Upsert one ORM object via session.merge(). Blocks until commit."""
        with self.transaction(op=op) as session:
            session.merge(obj)

    def stats(self) -> dict:
        """Snapshot of writer activity for diagnostics."""
        with self._stats_lock:
            return {
                "writes_attempted": self._writes_attempted,
                "writes_succeeded": self._writes_succeeded,
                "writes_rolled_back": self._writes_rolled_back,
                "wait_total_s": round(self._wait_total_s, 2),
                "hold_total_s": round(self._hold_total_s, 2),
                "avg_wait_ms": round(
                    1000 * self._wait_total_s / max(1, self._writes_attempted), 2
                ),
                "avg_hold_ms": round(
                    1000 * self._hold_total_s / max(1, self._writes_attempted), 2
                ),
            }


# Module-level singleton.
_instance: Optional[DBManager] = None
_instance_lock = threading.Lock()


def get_db_manager() -> DBManager:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = DBManager()
    return _instance
