"""Routine handler — nightly tagging of newly-formed beliefs.

The belief pipeline rebuilds the projection overnight, so new beliefs appear
untagged. This pass tags them from the standardized vocab (configs/belief_tags.yaml)
so consumers can pull them by tag (e.g. the meal engine). `only_untagged=True` is a
SHRINKING selector, so the pass converges to zero over runs; capped per run with
max_run_seconds as the watchdog, mirroring kg_importance_rater.

Corresponding routine entry at:
    configs/routines/public/belief_tag_new.json
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="belief_tag_new")
def belief_tag_new(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Tag active beliefs that have no tags yet (from the standardized vocab)."""
    from app.assistant.subconscious.belief_tagging import tag_beliefs

    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    max_per_run = int(spec.get("max_per_run", 60))

    summary = tag_beliefs(only_untagged=True, limit=max_per_run)
    logger.info("[belief_tag_new] tagged=%d domain_only=%d untagged=%d total=%d",
                summary.get("tagged", 0), summary.get("domain_only", 0),
                summary.get("untagged", 0), summary.get("total", 0))
    return {"status": "ok", **summary}
