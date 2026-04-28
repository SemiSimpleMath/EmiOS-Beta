"""
Ensure the belief-engine SQLite tables exist.

Run standalone:
    python -m belief_engine.db.ensure_schema
"""
from __future__ import annotations

import sqlite3
import logging
from pathlib import Path

from app.assistant.utils.path_utils import get_repo_root
from .schema import SCHEMA_SQL

logger = logging.getLogger(__name__)


def ensure_schema() -> None:
    db_path = get_repo_root() / "emi.db"
    logger.info("[belief_engine] Running migration against %s", db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info("[belief_engine] Migration complete.")
        print(f"[belief_engine] Migration OK — {db_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_schema()
