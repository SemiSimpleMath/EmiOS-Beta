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
    related_concern_id: Optional[str] = None,
    ask_mode: str = "chat",
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
            related_concern_id=(related_concern_id or None),
            ask_mode=(ask_mode or "chat").strip().lower() or "chat",
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


def get_asked_unanswered(*, max_age_hours: float = 72.0, limit: int = 20) -> List[PendingQuestion]:
    """Questions asked but not yet answered, newest first — the answer
    capture's working set. Questions older than `max_age_hours` are left
    for the noticer's expiry processing instead."""
    session = get_session()
    try:
        cutoff = utc_now() - timedelta(hours=float(max_age_hours))
        rows = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "asked")
            .filter(PendingQuestion.asked_at.isnot(None))
            .filter(PendingQuestion.asked_at >= cutoff)
            .order_by(PendingQuestion.asked_at.desc())
            .limit(limit)
            .all()
        )
        session.expunge_all()
        return rows
    finally:
        session.close()


def get_for_noticer_processing(*, stale_after_hours: float = 48.0) -> dict:
    """The noticer's question mailbox: captured answers awaiting processing
    plus stale asked-questions awaiting an expiry decision."""
    session = get_session()
    try:
        answered = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "answered")
            .order_by(PendingQuestion.answered_at.asc())
            .limit(20)
            .all()
        )
        stale_cutoff = utc_now() - timedelta(hours=float(stale_after_hours))
        stale = (
            session.query(PendingQuestion)
            .filter(PendingQuestion.status == "asked")
            .filter(PendingQuestion.asked_at.isnot(None))
            .filter(PendingQuestion.asked_at <= stale_cutoff)
            .order_by(PendingQuestion.asked_at.asc())
            .limit(20)
            .all()
        )
        session.expunge_all()
        return {"answered": answered, "stale_asked": stale}
    finally:
        session.close()


def mark_answered(
    question_id: str,
    *,
    answer_text: str,
    answer_message_id: Optional[str] = None,
) -> bool:
    """Answer captured (per-turn check or sweeper) — awaiting noticer
    processing. Refuses rows that aren't in 'asked'."""
    text = (answer_text or "").strip()
    if not text:
        logger.warning("[pending_questions] mark_answered with empty text id=%s", question_id)
        return False
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=question_id).first()
        if row is None or row.status != "asked":
            logger.warning(
                "[pending_questions] mark_answered skipped id=%s status=%s",
                question_id, getattr(row, "status", "missing"),
            )
            return False
        row.status = "answered"
        row.answered_at = utc_now()
        row.answer_text = text[:2000]
        if answer_message_id:
            row.answer_message_id = answer_message_id
        session.commit()
        logger.info("[pending_questions] answered id=%s", question_id[:8])
        return True
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] mark_answered failed id=%s", question_id)
        return False
    finally:
        session.close()


def close_question(question_id: str, *, outcome: str, notes: str = "") -> bool:
    """Noticer processed the question. outcome: 'closed' (answer handled)
    or 'expired' (no answer; stated default applied)."""
    if outcome not in ("closed", "expired"):
        logger.warning("[pending_questions] close_question bad outcome %r", outcome)
        return False
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=question_id).first()
        if row is None:
            return False
        row.status = outcome
        if notes:
            row.dismissed_reason = (notes or "")[:500]
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("[pending_questions] close_question failed id=%s", question_id)
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
