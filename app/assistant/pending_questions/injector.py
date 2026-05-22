"""QuestionInjector — appends a pending question to Emi's outbound reply.

Hooked from OutboundChatPublisher. Stateless per-call; all state lives
in the pending_question table.

Selection rules:
  1. Prefer questions tagged with the current conversation topic.
  2. Among matches, prefer 'high' priority, then oldest.
  3. If no topic match: fall back to the highest-priority oldest
     pending question regardless of tag.

Budget rules (against the 'asked' table window):
  - Default: max 5 asked per 24h. Configurable.
  - High-priority questions get one reserved slot per 24h that bypasses
    the regular budget.
  - Default: don't append two questions back-to-back (skip if the most
    recent asked_at is younger than min_minutes_between_asks).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple

from app.assistant.pending_questions.store import (
    count_asked_in_window,
    get_pending,
    mark_asked,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)


DEFAULT_DAILY_BUDGET = 5
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

    def maybe_append(
        self,
        *,
        text: str,
        topic_tag: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Decide whether to append a pending question to `text`.

        Returns ``(final_text, asked_question_id_or_None)``. The caller
        sends `final_text` to the user and (separately) can correlate
        the asked_question_id to the outbound message id if it wants
        the traceability stamp.

        Never raises into the caller. Failures log + return the original
        text untouched.
        """
        if not text or not text.strip():
            return text, None
        try:
            # Budget check first — cheap, common skip.
            asked_24h = count_asked_in_window(hours=24.0)
            if asked_24h >= self.daily_budget:
                # Daily budget hit. The one slot for high-priority can
                # still fire — let it through with a stricter source.
                pending = get_pending(topical_tag=topic_tag, limit=1)
                if not pending or pending[0].priority != "high":
                    pending = get_pending(limit=1)
                    if not pending or pending[0].priority != "high":
                        return text, None
            else:
                # Under budget. Prefer topic match, then fall back.
                pending = []
                if topic_tag:
                    pending = get_pending(topical_tag=topic_tag, limit=1)
                if not pending:
                    pending = get_pending(limit=1)
            if not pending:
                return text, None

            # Anti-back-to-back: skip if any question was asked very
            # recently. Bypassable by high-priority entries (they
            # should fire even mid-burst).
            if self.min_minutes_between > 0 and pending[0].priority != "high":
                recent = _seconds_since_last_ask()
                if recent is not None and recent < self.min_minutes_between * 60:
                    return text, None

            q = pending[0]
            tail = self._format_tail(q.question_text)
            new_text = f"{text.rstrip()}\n\n{tail}"
            mark_asked(q.id)
            logger.info(
                "[question_injector] appended question id=%s tag=%s priority=%s",
                q.id[:8], q.topical_tag, q.priority,
            )
            return new_text, q.id
        except Exception:
            logger.exception("[question_injector] failed, returning original text")
            return text, None

    @staticmethod
    def _format_tail(question_text: str) -> str:
        """Standard tail framing so the user always recognizes Emi's
        appended question and can ignore/answer at their leisure."""
        q = question_text.strip()
        if not q.endswith(("?", ".", "!")):
            q += "?"
        return f"— quick one if you have a sec: {q}"


# Module-level convenience that uses default config; OutboundChatPublisher
# imports this rather than instantiating per call.
_DEFAULT_INJECTOR = QuestionInjector()


def inject_question_into_reply(
    *,
    text: str,
    topic_tag: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Module-level convenience using the default injector."""
    return _DEFAULT_INJECTOR.maybe_append(text=text, topic_tag=topic_tag)


def _seconds_since_last_ask() -> Optional[float]:
    """How many seconds since the most recent asked question? None if
    never asked. Used for back-to-back gating."""
    from app.assistant.database.pending_question import PendingQuestion
    from app.models.base import get_session

    session = get_session()
    try:
        row = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "asked")
            .filter(PendingQuestion.asked_at.isnot(None))
            .order_by(PendingQuestion.asked_at.desc())
            .first()
        )
        if row is None or row.asked_at is None:
            return None
        return (utc_now() - row.asked_at).total_seconds()
    finally:
        session.close()
