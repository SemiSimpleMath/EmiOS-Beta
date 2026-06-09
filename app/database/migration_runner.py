"""Schema migration runner — applies numbered ALTER migrations on startup.

Design (see ``scratch/STREAM-A-SPEC.md`` §A3). The repo keeps
``Base.metadata.create_all(checkfirst=True)`` as the table CREATOR; this runner is
the authority for ALTERATIONS to existing tables (add column, index, backfill, data
fixups). It exists because auto-update is unsafe otherwise: an updated user on an
older DB would silently miss schema changes.

Going-forward convention (ALL contributors):
  - New table              -> register on Base.metadata; create_all picks it up.
  - Change existing table  -> ship a numbered ``migrations/NNNN_<slug>.py``.
    No more ad-hoc ALTER scripts.

A migration file exposes::

    def up(conn):   # conn is a sqlite3.Connection
        ...         # MUST be idempotent — guard with PRAGMA table_info(...) checks.

Runner contract:
  - Tracking table ``schema_migrations(id TEXT PRIMARY KEY, applied_at TEXT)``.
  - Migrations auto-discovered from ``migrations/`` by sorted filename.
  - Runs AFTER create_all, BEFORE serving. Each per-migration step commits on
    success; a failure rolls back that step and re-raises (fail loud).
  - **Baseline (critical):** on a FRESH db, create_all already built every table at
    the *latest* schema, so historical "add column" migrations must NOT run — they
    are STAMPED as applied instead. ``fresh_db`` is supplied by the caller, which
    knows whether the db file existed before create_all. (Idempotent ``up()`` makes
    running-on-fresh a no-op anyway, but stamping is cleaner + cheaper.)
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_TRACKING_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def default_migrations_dir() -> Path:
    """Migrations are code-layer assets (read-only, bundled with the app), so they
    anchor to the code root — NOT the writable data dir."""
    return get_repo_root() / "migrations"


def _discover(migrations_dir: Path) -> List[Tuple[str, Callable[[sqlite3.Connection], None]]]:
    """Return [(migration_id, up_callable)] sorted by filename. id = file stem."""
    if not migrations_dir.is_dir():
        return []
    out: List[Tuple[str, Callable]] = []
    for path in sorted(migrations_dir.glob("[0-9]*.py")):
        if path.name.startswith("__"):
            continue
        spec = importlib.util.spec_from_file_location(f"emi_migration_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load migration module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        up = getattr(module, "up", None)
        if not callable(up):
            raise AttributeError(f"migration {path.name} must define a callable up(conn)")
        out.append((path.stem, up))
    return out


def run_migrations(
    db_path: str | Path,
    *,
    fresh_db: bool,
    migrations_dir: Optional[Path] = None,
) -> List[str]:
    """Apply (or, on a fresh db, baseline-stamp) all pending migrations.

    Returns the list of migration ids that were newly recorded this run.
    """
    db_path = str(db_path)
    migrations_dir = Path(migrations_dir) if migrations_dir is not None else default_migrations_dir()

    conn = sqlite3.connect(db_path)
    newly_recorded: List[str] = []
    try:
        conn.execute(_TRACKING_DDL)
        conn.commit()
        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
        migrations = _discover(migrations_dir)

        # Backup-before-migration (R5): snapshot the DB only when REAL ALTER steps will run — an
        # EXISTING (non-fresh) db with pending migrations. A fresh db just baseline-stamps (no data to
        # lose) and an up-to-date db applies nothing, so neither needs a backup. This is the one place
        # a schema change touches a populated db (an updated user on an older DB); snapshot first so a
        # failed/destructive ALTER is recoverable. A backup failure aborts startup (fail loud).
        pending_ids = [mig_id for mig_id, _ in migrations if mig_id not in applied]
        if pending_ids and not fresh_db:
            from app.assistant.database.migration_backup import backup_database
            backup_database(reason=f"pre_migration_{pending_ids[0]}", db_path=db_path)

        for mig_id, up in migrations:
            if mig_id in applied:
                continue
            try:
                if fresh_db:
                    logger.info("[migrations] baseline (stamp, not run): %s", mig_id)
                else:
                    logger.info("[migrations] applying: %s", mig_id)
                    up(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                    (mig_id, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                newly_recorded.append(mig_id)
            except Exception:
                conn.rollback()
                logger.exception("[migrations] FAILED on %s — aborting startup", mig_id)
                raise

        if newly_recorded:
            verb = "baselined" if fresh_db else "applied"
            logger.info("[migrations] %s %d migration(s): %s", verb, len(newly_recorded), newly_recorded)
        else:
            logger.info("[migrations] up to date (%d known, %d already applied)",
                        len(migrations), len(applied))
        return newly_recorded
    finally:
        conn.close()
