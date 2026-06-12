"""pending_questions — Emi-initiated question queue + chat-context nudge.

Two pieces:

  store.py — enqueue/get/mark APIs. Any agent or pipeline can call
    `enqueue_question(...)` to add a question Emi should bring up next
    time she's in conversation with the user.

  injector.py — picks the best candidate when chat_gate is composing
    its reply. The question text becomes a nudge in chat_gate's
    context (NOT a mechanical post-process append); chat_gate decides
    whether to weave it into the natural reply.

Lifecycle of a question is on the PendingQuestion model
(app/assistant/database/pending_question.py).
"""
from app.assistant.pending_questions.store import (
    close_question,
    count_asked_in_window,
    enqueue_question,
    expire_stale,
    get_asked_unanswered,
    get_for_noticer_processing,
    get_pending,
    mark_answered,
    mark_asked,
    mark_dismissed,
)
from app.assistant.pending_questions.injector import (
    QuestionInjector,
    pick_question_for_nudge,
)

__all__ = [
    "close_question",
    "count_asked_in_window",
    "enqueue_question",
    "expire_stale",
    "get_asked_unanswered",
    "get_for_noticer_processing",
    "get_pending",
    "mark_answered",
    "mark_asked",
    "mark_dismissed",
    "QuestionInjector",
    "pick_question_for_nudge",
]
