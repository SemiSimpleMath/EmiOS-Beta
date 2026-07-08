"""QuestionInjector — picks a pending question for Emi's chat-reply
context. The chat agent sees it as a nudge and decides whether to
weave it into its natural reply (vs. mechanically appending).

This module's job is just:
  1. Pick the best candidate (topic match > priority > age).
  2. Apply budget / anti-back-to-back gates.
  3. Mark the picked question as asked so the same question doesn't
     resurface every turn. The noticer can re-emit if the underlying
     concern persists.

The agent gets the question text plus the "if_unanswered" default
inline; whether to actually surface it to the user is the agent's call.

Selection rules:
  1. Prefer questions tagged with the current conversation topic.
  2. Among matches, prefer 'high' priority, then oldest.
  3. If no topic match: fall back to the highest-priority oldest
     pending question regardless of tag.

Budget rules:
  - Default: max 6 asked per 24h (DEFAULT_DAILY_BUDGET).
  - Default: don't surface two questions back-to-back within
    min_minutes_between (10 min default).
  - 'high' priority bypasses both gates.
  - Both gates count by asked_at regardless of current status — an
    answered question still spent its slot.
"""
from __future__ import annotations

from typing import Optional, Tuple

from app.assistant.pending_questions.store import (
    count_asked_in_window,
    get_pending,
    mark_asked,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)


DEFAULT_DAILY_BUDGET = 6
DEFAULT_MIN_MINUTES_BETWEEN = 10.0


class QuestionInjector:
    def __init__(
        self,
        *,
        daily_budget: int = DEFAULT_DAILY_BUDGET,
        min_minutes_between: float = DEFAULT_MIN_MINUTES_BETWEEN,
    ) -> None:
        self.daily_budget = max(0, int(daily_budget))
        self.min_minutes_between = max(0.0, float(min_minutes_between))

    def pick_for_nudge(
        self,
        *,
        topic_tag: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """Pick a pending question to nudge the chat agent with.

        Returns ``(question_id, question_text)`` or None when nothing
        is eligible. Marks the picked question as asked.

        Never raises into the caller. Failures log + return None.
        """
        try:
            # Budget check.
            asked_24h = count_asked_in_window(hours=24.0)
            if asked_24h >= self.daily_budget:
                pending = []
                if topic_tag:
                    pending = get_pending(topical_tag=topic_tag, limit=1)
                if not pending or pending[0].priority != "high":
                    pending = get_pending(limit=1)
                    if not pending or pending[0].priority != "high":
                        return None
            else:
                pending = []
                if topic_tag:
                    pending = get_pending(topical_tag=topic_tag, limit=1)
                if not pending:
                    pending = get_pending(limit=1)
            if not pending:
                return None

            # Anti-back-to-back, bypassable by high priority.
            if self.min_minutes_between > 0 and pending[0].priority != "high":
                recent = _seconds_since_last_ask()
                if recent is not None and recent < self.min_minutes_between * 60:
                    return None

            q = pending[0]
            mark_asked(q.id)
            logger.info(
                "[question_injector] nudged question id=%s tag=%s priority=%s",
                q.id[:8], q.topical_tag, q.priority,
            )
            return (q.id, q.question_text)
        except Exception:
            logger.exception("[question_injector] pick_for_nudge failed")
            return None


# Module-level convenience using default config.
_DEFAULT_INJECTOR = QuestionInjector()


def pick_question_for_nudge(
    *, topic_tag: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    return _DEFAULT_INJECTOR.pick_for_nudge(topic_tag=topic_tag)


def _seconds_since_last_ask() -> Optional[float]:
    """How many seconds since the most recent ask? None if never asked.
    Used for back-to-back gating. Keys on asked_at regardless of current
    status — a question that was asked and instantly answered still counts
    as the most recent ask."""
    from app.assistant.database.pending_question import PendingQuestion
    from app.models.base import get_session

    session = get_session()
    try:
        row = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.asked_at.isnot(None))
            .order_by(PendingQuestion.asked_at.desc())
            .first()
        )
        if row is None or row.asked_at is None:
            return None
        return (utc_now() - row.asked_at).total_seconds()
    finally:
        session.close()
