"""Routine handler — nightly tagging of new + changed v1 beliefs.

The v1 belief pipeline rebuilds overnight, so new beliefs appear untagged and merged/evolved
beliefs have a changed statement. This pass tags active user_beliefs that are untagged OR stale
(their `updated_at` moved past their tags' `assigned_at`), from the standardized vocab
(configs/belief_tags.yaml), so consumers can pull them by tag. `mode="needs"` is a converging
selector; capped per run with max_run_seconds as the watchdog.

Corresponding routine entry: configs/routines/public/belief_tag_v1.json
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="belief_tag_v1")
def belief_tag_v1(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Tag active v1 beliefs that are untagged or stale (statement changed since last tagged)."""
    from belief_engine.tagging import tag_beliefs

    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    max_per_run = int(spec.get("max_per_run", 60))

    summary = tag_beliefs(mode="needs", limit=max_per_run)
    logger.info("[belief_tag_v1] tagged=%d domain_only=%d untagged=%d total=%d",
                summary.get("tagged", 0), summary.get("domain_only", 0),
                summary.get("untagged", 0), summary.get("total", 0))
    return {"status": "ok", **summary}
