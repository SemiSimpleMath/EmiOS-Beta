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


def _belief_db_path() -> str:
    """Belief tables live in the SAME db as the app. Resolve via the app's DB URI
    (honors DEV_DATABASE_URI_EMI — and EMI_DATA_DIR once the packaging path
    refactor lands) instead of hardcoding ``get_repo_root()/emi.db``, so belief and
    app always agree on which file they write. Local import avoids an import cycle."""
    try:
        from app.models.base import get_database_uri
        uri = get_database_uri()
        if uri.startswith("sqlite:///"):
            return uri[len("sqlite:///"):]
    except Exception:
        logger.exception("[belief_engine] could not resolve app DB URI; using legacy path")
    return str(get_repo_root() / "emi.db")


def ensure_schema() -> None:
    db_path = _belief_db_path()
    logger.info("[belief_engine] Running migration against %s", db_path)
    conn = sqlite3.connect(db_path)
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
