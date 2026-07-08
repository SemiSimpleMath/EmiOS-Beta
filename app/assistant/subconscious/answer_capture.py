"""Answer capture for noticer questions — closes the ask→answer loop.

When a noticer question is woven into chat, the user's reply must find
its way back to the subconscious. Two triggers share this one mechanism:

- per-turn: after a user message lands in a room with an open asked
  question (post-turn background check, off the reply path)
- hourly sweeper: the `subconscious_answer_sweep` routine catches answers
  given after restarts / in odd timing windows

Both call ``check_open_questions(...)``: candidate user messages since the
ask are judged by ``subconscious::answer_matcher`` (cheap model). A
captured answer marks the row `answered`, annotates the related concern's
journal immediately, and triggers a noticer tick (cooldown-guarded) so the
register reacts within minutes instead of at tomorrow's 04:00 tick.

The sweep is FREE in the common case: no asked-unanswered questions means
pure SQL, no LLM call.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pending_questions import get_asked_unanswered, mark_answered
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_CANDIDATE_MESSAGE_CAP = 12

# A captured answer triggers an immediate noticer tick, but never more than
# once per cooldown window (the tick reads ALL captured answers anyway).
_NOTICER_TRIGGER_COOLDOWN_S = 600
_noticer_trigger_lock = threading.Lock()
_last_noticer_trigger_monotonic: float = 0.0


def _resolve_asked_room(asked_in_message_id: str) -> Optional[str]:
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session

    session = get_session()
    try:
        row = (
            session.query(UnifiedLog2026.room_id)
            .filter(UnifiedLog2026.id == asked_in_message_id)
            .first()
        )
        return str(row.room_id) if row and row.room_id else None
    finally:
        session.close()


def _candidate_messages(*, room_id: str, since_utc: datetime) -> List[Dict[str, Any]]:
    """User messages in the room since the ask, oldest first."""
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session

    session = get_session()
    try:
        rows = (
            session.query(
                UnifiedLog2026.id,
                UnifiedLog2026.timestamp,
                UnifiedLog2026.message,
            )
            .filter(UnifiedLog2026.room_id == room_id)
            .filter(UnifiedLog2026.role == "user")
            .filter(UnifiedLog2026.timestamp > since_utc)
            .order_by(UnifiedLog2026.timestamp.asc())
            .limit(_CANDIDATE_MESSAGE_CAP)
            .all()
        )
        return [
            {
                "id": str(r.id),
                "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                "text": str(r.message or "")[:600],
            }
            for r in rows
        ]
    finally:
        session.close()


def _concern_title(concern_id: Optional[str]) -> str:
    if not concern_id:
        return ""
    from app.assistant.subconscious.digest_runner import load_register

    register = load_register()
    for bucket in ("active", "addressing", "resolved", "dormant"):
        for c in register.get(bucket) or []:
            if c.get("concern_id") == concern_id:
                return str(c.get("title") or "")
    return ""


def _judge(question, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One answer_matcher call. Returns the verdict dict or None on failure."""
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    agent_input = {
        "question_text": question.question_text,
        "asked_at": question.asked_at.isoformat() if question.asked_at else "",
        "why_asking": "",
        "related_concern_title": _concern_title(question.related_concern_id),
        "candidate_messages": candidates,
    }
    return _run_subconscious_agent(
        handler_label="answer_matcher",
        agent_name="subconscious::answer_matcher",
        context=agent_input,
        scope_id="subconscious::answer_matcher",
        actor_id="subconscious::answer_capture",
    )


def annotate_concern_answer(concern_id: str, *, question_text: str, answer_text: str) -> bool:
    """Journal the captured answer onto the concern immediately — the
    noticer formally processes it on its (triggered) next tick. Delegates to
    persist.annotate_concern_answer: ALL register writes live in persist
    behind one lock and one atomic writer."""
    from app.assistant.subconscious.persist import annotate_concern_answer as _annotate
    return _annotate(concern_id, question_text=question_text, answer_text=answer_text)


def trigger_noticer(reason: str) -> bool:
    """Fire one noticer tick in a background thread (cooldown-guarded)."""
    global _last_noticer_trigger_monotonic
    with _noticer_trigger_lock:
        now = time.monotonic()
        if now - _last_noticer_trigger_monotonic < _NOTICER_TRIGGER_COOLDOWN_S:
            logger.info("[answer_capture] noticer trigger suppressed (cooldown): %s", reason)
            return False
        _last_noticer_trigger_monotonic = now

    def _run():
        try:
            from app.assistant.routine_handlers.subconscious import noticer_run
            summary = noticer_run()
            logger.info("[answer_capture] triggered noticer tick (%s): %s", reason, summary)
        except Exception:
            logger.exception("[answer_capture] triggered noticer tick failed (%s)", reason)

    threading.Thread(target=_run, name="noticer-answer-trigger", daemon=True).start()
    return True


def check_open_questions(*, room_id: Optional[str] = None) -> Dict[str, Any]:
    """Judge every asked-unanswered question (optionally one room only).

    Returns a summary dict. No open questions → pure SQL, zero LLM calls.
    """
    questions = get_asked_unanswered()
    if room_id:
        scoped = []
        for q in questions:
            if not q.asked_in_message_id:
                continue
            q_room = _resolve_asked_room(q.asked_in_message_id)
            if q_room == room_id:
                scoped.append(q)
        questions = scoped
    if not questions:
        return {"checked": 0, "answered": 0}

    answered = 0
    for q in questions:
        if not q.asked_in_message_id or not q.asked_at:
            logger.info(
                "[answer_capture] question %s has no ask anchor; leaving for expiry",
                q.id[:8],
            )
            continue
        q_room = _resolve_asked_room(q.asked_in_message_id)
        if not q_room:
            continue
        candidates = _candidate_messages(room_id=q_room, since_utc=q.asked_at)
        if not candidates:
            continue

        verdict = _judge(q, candidates)
        if not isinstance(verdict, dict):
            continue
        if verdict.get("verdict") != "answered":
            logger.info(
                "[answer_capture] question %s verdict=%s (%s)",
                q.id[:8], verdict.get("verdict"), verdict.get("notes", ""),
            )
            continue
        answer_text = str(verdict.get("answer_text") or "").strip()
        if not answer_text:
            logger.warning("[answer_capture] answered verdict without text for %s", q.id[:8])
            continue

        ok = mark_answered(
            q.id,
            answer_text=answer_text,
            answer_message_id=verdict.get("answer_message_id"),
        )
        if not ok:
            continue
        answered += 1
        if q.related_concern_id:
            annotate_concern_answer(
                q.related_concern_id,
                question_text=q.question_text,
                answer_text=answer_text,
            )

    if answered:
        trigger_noticer(f"{answered} answer(s) captured")
    return {"checked": len(questions), "answered": answered}
