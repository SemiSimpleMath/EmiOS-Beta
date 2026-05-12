"""
pipeline.py — End-to-end context_engine pipeline runner.

Stages in order:
    1. chat_scan           (sync, ~1s)   — decide whether to activate
    2. context_activation  (sync, ~15s)  — KG + LLM dossier
    3. situation_brief     (sync, ~0s)   — persist to disk
    4. reasoning_agent     (sync, ~60s)  — research + proactive emit

The full pipeline is designed to be dispatched as a background thread from
room_session_manager so it does not block the main chat response.

Usage (background dispatch):
    from app.assistant.context_engine.pipeline import maybe_trigger_pipeline

    # Called from room_session_manager after the main response is sent.
    maybe_trigger_pipeline(
        user_message=envelope.body,
        primary_user="Jukka",
        owner_id=envelope.room_id,
    )

Direct usage (blocking, for tests):
    from app.assistant.context_engine.pipeline import run_context_activation_pipeline

    result = run_context_activation_pipeline(
        user_message="Rahul is coming to visit",
        primary_user="Jukka",
        owner_id="jukka",
    )
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Guards against concurrent pipeline runs for the same owner.
# A second incoming message while the pipeline is running is simply dropped.
_running_locks: dict[str, threading.Lock] = {}
_running_locks_guard = threading.Lock()


def _get_owner_lock(owner_id: str) -> threading.Lock:
    with _running_locks_guard:
        if owner_id not in _running_locks:
            _running_locks[owner_id] = threading.Lock()
        return _running_locks[owner_id]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_context_activation_pipeline(
    *,
    user_message: str,
    primary_user: str,
    owner_id: str,
    recent_chat_context: Optional[str] = None,
    skip_scan: bool = False,
) -> Optional[object]:
    """
    Run the full context_engine pipeline synchronously.

    Args:
        user_message:           The user's chat message.
        primary_user:           Name of the primary user (e.g. "Jukka").
        owner_id:               Room / KG owner identifier.
        recent_chat_context:    Optional recent chat text passed to chat_scan.
        skip_scan:              If True, skip chat_scan and force activation.
        emit_proactive_message: Whether to emit a proactive chat message when done.

    Returns:
        The completed SituationBrief (with reasoning_output populated), or None
        if chat_scan decided not to activate.

    Raises:
        Any exception from the sub-stages propagates — no silent fallback.
    """
    from app.assistant.context_engine.chat_scan import scan_for_activation
    from app.assistant.context_engine.context_activation import run_context_activation
    from app.assistant.context_engine.situation_brief import SituationBrief, save_brief
    from app.assistant.context_engine.reasoning_agent import run_reasoning_agent
    from app.assistant.context_engine.context_memo import distil_memo, write_memo_to_blackboard

    t_total = time.perf_counter()

    # ── Stage 1: chat_scan ────────────────────────────────────────────────────
    if not skip_scan:
        logger.debug("pipeline: chat_scan starting")
        scan = scan_for_activation(
            user_message=user_message,
            primary_user=primary_user,
            recent_chat_context=recent_chat_context,
            owner_id=owner_id,
        )
        logger.debug(
            "pipeline: chat_scan result should_activate=%s seeds=%r reason=%r",
            scan.should_activate,
            scan.seeds,
            scan.reason,
        )
        if not scan.should_activate:
            return None
        seeds = scan.seeds
    else:
        seeds = [primary_user]
        logger.debug("pipeline: skip_scan=True — seeds default to primary_user only; caller should pass seeds explicitly")

    # ── Stage 2: context_activation ──────────────────────────────────────────
    logger.debug("pipeline: context_activation starting seeds=%r", seeds)
    activation = run_context_activation(
        seeds=seeds,
        user_message=user_message,
        primary_user=primary_user,
        owner_id=owner_id,
    )
    logger.debug(
        "pipeline: context_activation done in %.1fs",
        activation.elapsed_seconds,
    )

    # ── Stage 3: persist brief ────────────────────────────────────────────────
    brief = SituationBrief(
        owner_id=owner_id,
        user_message=user_message,
        seeds=seeds,
        kg_briefing=activation.kg_briefing,
        situation_brief=activation.situation_brief,
        activation_elapsed_seconds=activation.elapsed_seconds,
    )
    save_brief(brief)
    logger.debug("pipeline: brief saved brief_id=%s", brief.brief_id)

    # ── Stage 4: reasoning_agent ──────────────────────────────────────────────
    logger.debug("pipeline: reasoning_agent starting brief_id=%s", brief.brief_id)
    t_reasoning = time.perf_counter()
    reasoning_output = run_reasoning_agent(
        brief=brief,
        primary_user=primary_user,
        owner_id=owner_id,
    )
    brief.reasoning_output = reasoning_output
    brief.reasoning_elapsed_seconds = time.perf_counter() - t_reasoning

    # Update persisted brief with reasoning output
    save_brief(brief)

    # ── Stage 5: distil memo + write to blackboard ────────────────────────────
    logger.debug("pipeline: distilling memo brief_id=%s", brief.brief_id)
    try:
        memo_text = distil_memo(
            reasoning_output=reasoning_output,
            user_message=user_message,
            primary_user=primary_user,
        )
        write_memo_to_blackboard(
            memo_text=memo_text,
            brief_id=brief.brief_id,
            user_message=user_message,
            seeds=seeds,
        )
        logger.debug("pipeline: memo written brief_id=%s", brief.brief_id)
    except Exception as e:
        # Memo distillation is the last stage and not load-bearing — the
        # pipeline's earlier output (brief, reasoning_result) is still useful.
        # Log + continue so a memo bug doesn't lose that work.
        logger.error("pipeline: memo distillation failed (non-fatal) brief_id=%s: %s", brief.brief_id, e)
        logger.debug("pipeline: memo distillation exception details", exc_info=True)

    total_elapsed = time.perf_counter() - t_total
    logger.info(
        "pipeline: complete brief_id=%s total=%.1fs activation=%.1fs reasoning=%.1fs",
        brief.brief_id,
        total_elapsed,
        activation.elapsed_seconds,
        brief.reasoning_elapsed_seconds,
    )
    return brief


def maybe_trigger_pipeline(
    *,
    user_message: str,
    primary_user: str,
    owner_id: str,
    recent_chat_context: Optional[str] = None,
) -> bool:
    """
    Fire the pipeline as a background thread if not already running for this owner.

    Returns True if a background thread was started, False if one was already running
    (the new request is dropped to avoid duplicate parallel runs).

    The pipeline runs the chat_scan first; if scan returns should_activate=False
    the thread exits cheaply after ~1s.
    """
    lock = _get_owner_lock(owner_id)

    if not lock.acquire(blocking=False):
        logger.debug(
            "pipeline: context_engine already running for owner_id=%r — skipping duplicate trigger",
            owner_id,
        )
        return False

    def _background():
        try:
            run_context_activation_pipeline(
                user_message=user_message,
                primary_user=primary_user,
                owner_id=owner_id,
                recent_chat_context=recent_chat_context,
            )
        except Exception as e:
            logger.error(
                "pipeline: background run failed owner_id=%r: %s", owner_id, e, exc_info=True
            )
            raise
        finally:
            lock.release()

    t = threading.Thread(
        target=_background,
        name=f"context_engine_{owner_id}",
        daemon=True,
    )
    t.start()
    logger.debug("pipeline: background thread started for owner_id=%r", owner_id)
    return True
