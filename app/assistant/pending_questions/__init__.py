"""pending_questions — Emi-initiated question queue + chat injector.

Two pieces:

  store.py — enqueue/get/mark APIs. Any agent or pipeline can call
    `enqueue_question(...)` to add a question Emi should ask the user
    next time the conversation surfaces.

  injector.py — the consumer. Hooked into OutboundChatPublisher so
    that when Emi sends a chat reply, a topically-matching pending
    question gets appended as a "P.S." tail (one per reply, budgeted
    across a rolling window).

Lifecycle of a question is on the PendingQuestion model
(app/assistant/database/pending_question.py).
"""
from app.assistant.pending_questions.store import (
    enqueue_question,
    get_pending,
    mark_asked,
    mark_dismissed,
    expire_stale,
    count_asked_in_window,
)
from app.assistant.pending_questions.injector import (
    QuestionInjector,
    inject_question_into_reply,
)

__all__ = [
    "enqueue_question",
    "get_pending",
    "mark_asked",
    "mark_dismissed",
    "expire_stale",
    "count_asked_in_window",
    "QuestionInjector",
    "inject_question_into_reply",
]
