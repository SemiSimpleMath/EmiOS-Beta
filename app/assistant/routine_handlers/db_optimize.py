"""Nightly SQLite planner maintenance.

Persistence audit P2 (2026-07-09): emi.db had 103 tables and 351 indexes
and ANALYZE had never run — no sqlite_stat1, so the query planner chose
plans from schema shape alone. ``PRAGMA optimize`` is SQLite's documented
long-running-app pattern: it re-ANALYZEs only tables whose statistics
look stale, so the nightly run is a no-op most nights and cheap when it
isn't. ``analysis_limit`` bounds the per-index row sampling so even a
full pass stays in seconds on a ~750MB file.
"""
from __future__ import annotations

from typing import Any

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="db_optimize")
def db_optimize(*, target_date=None, routine=None, event_message=None) -> dict[str, Any]:
    from sqlalchemy import text

    from app.models.db_manager import get_db_manager

    with get_db_manager().transaction(op="db_optimize") as session:
        session.execute(text("PRAGMA analysis_limit=1000"))
        session.execute(text("PRAGMA optimize"))
    logger.info("[db_optimize] PRAGMA optimize completed")
    return {"status": "ok"}
