"""Routine handler — nightly eviction of deprecated beliefs to the archive tables.

Deprecation (decay + canonicalize merges, overnight) only flips status; this sweep is the
lifecycle terminus that moves the newly-deprecated beliefs OUT of the live tables, so the live
store stays lean by construction instead of accumulating dead rows. Idempotent.

Corresponding routine entry: configs/routines/public/belief_archive.json
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="belief_archive")
def belief_archive(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Evict status='deprecated' beliefs (+ their evidence) into the archive tables."""
    from belief_engine.archive import archive_deprecated_beliefs

    summary = archive_deprecated_beliefs()
    logger.info("[belief_archive] archived beliefs=%d evidence=%d",
                summary.get("beliefs", 0), summary.get("evidence", 0))
    return {"status": "ok", **summary}
