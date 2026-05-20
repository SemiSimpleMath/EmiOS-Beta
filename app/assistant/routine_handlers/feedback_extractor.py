"""Routine handler — daily drain of queued feedback.comment pods.

Wraps the CLI flow in app/assistant/subconscious/run_feedback_extractor.py
so it can run as a declarative routine (interval, active_window,
on_error) without anyone typing the command. Designed for once-daily
cadence inside the belief-engine rhythm: the existing belief pipeline
recomputes decay overnight; this hands fresh user-comment evidence
into the same store so the morning's snapshot reflects it.

Corresponding routine entry at:
    configs/routines/public/feedback_extractor_daily.json
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="feedback_extractor_run")
def feedback_extractor_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Run one feedback_extractor pass.

    Returns a small summary so the routine_manager's decision log
    records what happened (counts of extractions, upserts, skips)."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.subconscious.feedback_extractor_context_builder import (
        build_feedback_extractor_context,
    )
    from app.assistant.subconscious.feedback_extractor_persist import (
        apply_feedback_extractor_output,
    )
    from app.assistant.utils.pydantic_classes import (
        Message,
        ScopeContext,
        ScopeResourcePolicy,
    )

    context = build_feedback_extractor_context()

    # Fast path: if there are no unprocessed comments, skip the LLM call.
    block = context.get("unprocessed_comments_block", "")
    if "no unprocessed feedback.comment pods" in block:
        logger.info("[feedback_extractor_run] queue empty — no work")
        return {"status": "ok", "queue_empty": True}

    agent = DI.agent_factory.create_agent("feedback_extractor")
    if agent is None:
        logger.error("[feedback_extractor_run] could not create feedback_extractor agent")
        return {"status": "error", "error": "agent_unavailable"}

    scope = ScopeContext(
        scope_id="subconscious::feedback_extractor",
        owner_id="system",
        actor_id="routine::feedback_extractor_run",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    try:
        result = agent.action_handler(Message(agent_input=context, scope_context=scope))
    except Exception as e:
        logger.exception("[feedback_extractor_run] agent raised: %s", e)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    output: Dict[str, Any] = {}
    if hasattr(result, "data") and isinstance(result.data, dict):
        output = result.data
    elif isinstance(result, dict):
        output = result
    else:
        logger.warning("[feedback_extractor_run] unexpected result type: %s", type(result).__name__)
        return {"status": "error", "error": "bad_result_shape"}

    summary = apply_feedback_extractor_output(output)
    logger.info(
        "[feedback_extractor_run] extractions=%d upserted=%d skipped=%d marked_processed=%d",
        summary.get("extractions_count", 0),
        summary.get("upserted_count", 0),
        summary.get("skipped_count", 0),
        summary.get("comments_marked_processed", 0),
    )
    return {"status": "ok", **summary}
