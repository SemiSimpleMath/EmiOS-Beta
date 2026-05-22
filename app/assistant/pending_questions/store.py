"""CRUD layer for the pending_question queue.

Sessions are short-lived. Each function opens, does its work, closes —
never holds a session across an LLM call or external I/O.
"""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from sqlalchemy import or_

from app.assistant.database.pending_question import PendingQuestion
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)


_VALID_PRIORITIES = {"low", "medium", "high"}


def enqueue_question(
    *,
    question_text: str,
    topical_tag: str = "general",
    priority: str = "medium",
    created_by: Optional[str] = None,
    expires_after_hours: Optional[float] = 72.0,
) -> Optional[str]:
    """Add a new pending question. Returns the question id, or None on
    bad input (caller's logged).

    `expires_after_hours` defaults to 3 days so a question that never
    gets asked doesn't linger forever. Pass None for no expiration.
    """
    text = (question_text or "").strip()
    if not text:
        logger.debug("[pending_questions] empty question text; not enqueued")
        return None
    pri = (priority or "medium").strip().lower()
    if pri not in _VALID_PRIORITIES:
        logger.warning(
            "[pending_questions] invalid priority %r; defaulting to medium", priority,
        )
        pri = "medium"
    tag = (topical_tag or "general").strip().lower() or "general"

    expires_at = None
    if expires_after_hours is not None and expires_after_hours > 0:
        expires_at = utc_now() + timedelta(hours=float(expires_after_hours))

    session = get_session()
    try:
        row = PendingQuestion(
            question_text=text,
            topical_tag=tag,
            priority=pri,
            status="pending",
            created_by=(created_by or "unknown")[:128],
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        logger.info(
            "[pending_questions] enqueued id=%s tag=%s priority=%s by=%s",
            row.id[:8], tag, pri, created_by,
        )
        return row.id
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] enqueue failed")
        return None
    finally:
        session.close()


def get_pending(
    *,
    topical_tag: Optional[str] = None,
    limit: int = 20,
) -> List[PendingQuestion]:
    """Return pending questions in pick-order: priority (high → low),
    then oldest first within priority.

    If `topical_tag` is given, restricts to that tag.
    Expired rows are skipped (and flipped to 'expired' by the caller
    via `expire_stale()` — get_pending stays a pure read).
    """
    session = get_session()
    try:
        now = utc_now()
        q = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "pending")
            .filter(
                or_(
                    PendingQuestion.expires_at.is_(None),
                    PendingQuestion.expires_at > now,
                )
            )
        )
        if topical_tag:
            q = q.filter(PendingQuestion.topical_tag == topical_tag.lower())
        # Priority order: high < medium < low alphabetically would be
        # wrong, so sort by an explicit case expression.
        from sqlalchemy import case
        priority_order = case(
            (PendingQuestion.priority == "high", 0),
            (PendingQuestion.priority == "medium", 1),
            else_=2,
        )
        q = q.order_by(priority_order.asc(), PendingQuestion.created_at.asc())
        rows = q.limit(limit).all()
        # Detach to make safe to use after session close
        session.expunge_all()
        return rows
    finally:
        session.close()


def mark_asked(question_id: str, *, asked_in_message_id: Optional[str] = None) -> bool:
    """Flip status to 'asked' and stamp asked_at."""
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=question_id).first()
        if row is None:
            return False
        row.status = "asked"
        row.asked_at = utc_now()
        if asked_in_message_id:
            row.asked_in_message_id = asked_in_message_id
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] mark_asked failed id=%s", question_id)
        return False
    finally:
        session.close()


def mark_dismissed(question_id: str, *, reason: str = "") -> bool:
    """Cancel a pending question without asking. Reason is audit-only."""
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=question_id).first()
        if row is None:
            return False
        row.status = "dismissed"
        row.dismissed_reason = (reason or "")[:500]
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] mark_dismissed failed id=%s", question_id)
        return False
    finally:
        session.close()


def expire_stale() -> int:
    """Sweep: flip any pending+past-expires_at rows to 'expired'.
    Returns count flipped. Call from a low-cadence maintenance routine.
    """
    session = get_session()
    try:
        now = utc_now()
        n = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "pending")
            .filter(PendingQuestion.expires_at.isnot(None))
            .filter(PendingQuestion.expires_at <= now)
            .update({"status": "expired"}, synchronize_session=False)
        )
        session.commit()
        if n:
            logger.info("[pending_questions] expired %d stale row(s)", n)
        return int(n or 0)
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] expire_stale failed")
        return 0
    finally:
        session.close()


def count_asked_in_window(hours: float = 24.0) -> int:
    """How many questions have been asked in the last `hours`?
    Used by the injector to enforce a daily budget.
    """
    session = get_session()
    try:
        now = utc_now()
        cutoff = now - timedelta(hours=float(hours))
        return int(
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "asked")
            .filter(PendingQuestion.asked_at.isnot(None))
            .filter(PendingQuestion.asked_at >= cutoff)
            .count()
        )
    finally:
        session.close()
