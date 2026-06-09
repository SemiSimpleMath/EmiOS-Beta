"""Backup-before-migration hook (reliability R5).

A schema migration can corrupt or destroy data, so emi.db is snapshotted to a restorable copy BEFORE
a migration alters an existing db. backup_database() takes a SQLite-consistent online snapshot (safe
even while the app is writing — unlike a raw file copy, which can tear a WAL-mode db) into the
gitignored repo_root/backups/ dir (contains the full user DB = PII; never committed), pruned to the
last few.

PRIMARY integration: the schema migration runner (app/database/migration_runner.py) calls
backup_database() before applying pending ALTER steps to an EXISTING db. A fresh install builds the
current schema directly via create_all and baseline-stamps the migrations (no data at risk, no
backup), so the snapshot only happens on the one path that matters — an updated user on an older db.

run_with_backup() is a convenience for ad-hoc / standalone migration scripts run by hand: backup
THEN run; a backup failure aborts (we never migrate a db we can't roll back — fail loud).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

DEFAULT_KEEP = 5   # migration backups are rare but large (the whole DB); keep the last few


def _default_db_path() -> Path:
    return get_repo_root() / "emi.db"


def _default_backups_dir() -> Path:
    # repo_root/backups/ is gitignored (.gitignore: /backups/) and purpose-named for migration backups.
    return get_repo_root() / "backups"


def _prune_old_backups(backups_dir: Path, *, keep: int) -> None:
    snaps = sorted(
        backups_dir.glob("emi.db.*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in snaps[keep:]:
        try:
            old.unlink()
            logger.info("backup_database: pruned old snapshot %s", old.name)
        except Exception as e:
            logger.warning("backup_database: could not prune %s: %s", old, e)


def backup_database(
    reason: str = "migration",
    *,
    db_path: Optional[Path] = None,
    backups_dir: Optional[Path] = None,
    keep: int = DEFAULT_KEEP,
) -> Optional[Path]:
    """Snapshot emi.db to a timestamped, restorable copy; return its path.

    Returns None when there is no DB yet (a CREATE migration on a fresh install — nothing to back up).
    Raises if the snapshot operation itself fails (disk full, sqlite error) so the caller can abort
    the migration rather than proceed without a rollback point.
    """
    db_path = Path(db_path) if db_path is not None else _default_db_path()
    backups_dir = Path(backups_dir) if backups_dir is not None else _default_backups_dir()

    if not db_path.exists():
        logger.info("backup_database: no DB at %s — nothing to back up (fresh install?)", db_path)
        return None

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason).strip("_")[:48] or "migration"
    dest = backups_dir / f"emi.db.{ts}.{safe_reason}.bak"

    # SQLite online backup — a consistent snapshot even if another connection is mid-write.
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size_mb = dest.stat().st_size / (1024 * 1024)
    logger.info("backup_database: snapshot [%s] -> %s (%.1f MB)", reason, dest, size_mb)
    _prune_old_backups(backups_dir, keep=keep)
    return dest


def run_with_backup(migrate_fn: Callable[[], Any], *, name: str = "migration") -> Any:
    """Snapshot emi.db, THEN run the migration entrypoint. The hook: no schema migration runs without
    a restorable snapshot first. If the snapshot fails, the exception propagates and the migration does
    NOT run — we never migrate a DB we can't roll back (fail loud)."""
    backup_database(reason=name)
    return migrate_fn()
