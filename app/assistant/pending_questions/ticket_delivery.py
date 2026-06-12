"""Ticket-mode delivery for pending questions.

High-stakes noticer questions (ask_mode='ticket': high severity, near
horizon) deserve the big modal instead of waiting for a chat slot — and
the modal gives PERFECT answer attribution: the typed response comes back
bound to the ticket, whose trigger_context carries the question_id.

Delivery: ``deliver_question_as_ticket`` creates an `ask_user` ticket
(the UI's question layout: text box + Submit/Skip), marks it proposed,
pushes the proactive_suggestion popup event, and flips the question to
'asked' with asked_in_message_id='ticket:<id>' — which also removes it
from the chat injector's pool so the user is never asked twice. The
'ticket:' anchor makes the chat-side answer matcher skip it (no chat room
resolves from it).

Answer routing: ``register_ticket_answer_listener`` (called once at boot)
subscribes to 'dayflow_ticket_responded'. A response to a question ticket
routes straight into the same answer loop the chat path uses: row marked
answered with the user's text, related concern journaled, noticer tick
triggered. A dismissed ticket expires the question immediately — the
user explicitly declined, no need to wait for the 48h mailbox.
"""
from __future__ import annotations

from typing import Any, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_LISTENER_REGISTERED = False


def deliver_question_as_ticket(
    *,
    question_id: str,
    question_text: str,
    priority: str = "high",
) -> Optional[str]:
    """Create + propose an ask_user ticket for one pending question.

    Returns the ticket_id, or None on failure (the question then stays
    'pending' and the chat injector will deliver it on its normal path —
    logged at ERROR so a broken ticket pipeline is visible).
    """
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.ticket_manager.ticket_manager import get_ticket_manager
        from app.assistant.utils.pydantic_classes import Message

        ticket_manager = get_ticket_manager()
        ticket = ticket_manager.create_ticket(
            ticket_type="ask_user",
            suggestion_type="subconscious_question",
            title=question_text[:80],
            message=question_text,
            action_type="none",
            trigger_context={
                "question_id": question_id,
                "source": "subconscious::noticer",
            },
            trigger_reason="subconscious noticer question (ticket-mode)",
            valid_hours=48,
        )
        if ticket is None:
            raise RuntimeError("ticket_manager.create_ticket returned None")
        if not ticket_manager.mark_proposed(ticket.ticket_id):
            raise RuntimeError(f"mark_proposed failed for {ticket.ticket_id}")

        DI.event_hub.publish(Message(
            event_topic="proactive_suggestion",
            data=ticket.to_dict(),
        ))

        from app.assistant.pending_questions.store import mark_asked
        mark_asked(question_id, asked_in_message_id=f"ticket:{ticket.ticket_id}")
        logger.info(
            "[ticket_delivery] question %s delivered as ticket %s",
            question_id[:8], ticket.ticket_id,
        )
        return ticket.ticket_id
    except Exception:
        logger.exception(
            "[ticket_delivery] FAILED delivering question %s as ticket; "
            "it stays pending for the chat injector", question_id[:8],
        )
        return None


def _on_ticket_responded(message: Any) -> None:
    """EventHub callback: route question-ticket responses into the answer loop."""
    try:
        data = getattr(message, "data", None) or {}
        ticket_id = str(data.get("ticket_id") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        user_text = str(data.get("user_text") or "").strip()
        if not ticket_id:
            return

        from app.assistant.ticket_manager.ticket_manager import get_ticket_manager
        ticket = get_ticket_manager().get_ticket_by_id(ticket_id)
        if ticket is None:
            return
        trigger_context = getattr(ticket, "trigger_context", None) or {}
        question_id = str(trigger_context.get("question_id") or "").strip()
        if not question_id:
            return  # not a question ticket — none of our business

        from app.assistant.pending_questions.store import close_question, mark_answered

        if action in ("answer", "accept") and user_text:
            ok = mark_answered(
                question_id,
                answer_text=user_text,
                answer_message_id=f"ticket:{ticket_id}",
            )
            if not ok:
                logger.warning(
                    "[ticket_delivery] mark_answered refused for question %s "
                    "(ticket %s)", question_id[:8], ticket_id,
                )
                return
            # Same downstream as the chat path: journal + immediate tick.
            session_row = None
            from app.models.base import get_session
            from app.assistant.database.pending_question import PendingQuestion
            session = get_session()
            try:
                session_row = session.query(PendingQuestion).filter_by(id=question_id).first()
                session.expunge_all()
            finally:
                session.close()
            from app.assistant.subconscious.answer_capture import (
                annotate_concern_answer,
                trigger_noticer,
            )
            if session_row is not None and session_row.related_concern_id:
                annotate_concern_answer(
                    session_row.related_concern_id,
                    question_text=session_row.question_text,
                    answer_text=user_text,
                )
            trigger_noticer(f"ticket answer for question {question_id[:8]}")
            logger.info(
                "[ticket_delivery] ticket %s answered question %s",
                ticket_id, question_id[:8],
            )
        elif action in ("dismiss", "dismissed", "skip", "reject"):
            close_question(question_id, outcome="expired", notes=f"ticket {ticket_id} dismissed by user")
            logger.info(
                "[ticket_delivery] ticket %s dismissed — question %s expired",
                ticket_id, question_id[:8],
            )
        # snooze and other actions: leave the question 'asked'; the ticket
        # resurfaces on its own schedule.
    except Exception:
        logger.exception("[ticket_delivery] ticket response routing failed")


def register_ticket_answer_listener() -> None:
    """Subscribe the answer router to ticket responses. Call once at boot."""
    global _LISTENER_REGISTERED
    if _LISTENER_REGISTERED:
        return
    from app.assistant.ServiceLocator.service_locator import DI
    DI.event_hub.register_event("dayflow_ticket_responded", _on_ticket_responded)
    _LISTENER_REGISTERED = True
    logger.info("[ticket_delivery] ticket answer listener registered")
