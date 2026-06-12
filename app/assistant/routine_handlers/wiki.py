"""Routine handlers for the wiki generator's learning loop."""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="wiki_fact_drain_run")
def wiki_fact_drain_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """One synthetic-fact drain pass: process captured answers (confirmed
    facts → investigated/auto_apply for the executor), expire stale asks,
    then gate new proposals into confirmation questions (budgeted)."""
    from app.assistant.wiki_generator.synthetic_fact_drain import run_fact_drain

    summary = run_fact_drain()
    return {"status": "ok", **summary}
