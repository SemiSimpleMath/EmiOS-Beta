"""PendingQuestion — queue of questions Emi wants to ask the user.

Populated by any agent or pipeline step that notices a gap, ambiguity,
or decision waiting on user input. Consumed by the question_injector,
which appends the highest-priority topically-matching question to
Emi's outbound chat replies (one question per reply, budgeted across
a rolling window so the user doesn't get hammered).

Lifecycle (answer loop closed 2026-06-11):
    pending -> asked -> answered -> closed
                  |         (answer captured       (noticer processed the
                  |          by the per-turn check   answer: concern updated,
                  |          or hourly sweeper)      question retired)
                  +-> expired (no answer in the freshness window; the
                      noticer applies the question's stated default)
    pending -> dismissed (creator or maintenance cancelled before ask)
    pending -> expired (freshness window passed before it was asked)

The queue is intentionally simple — no LLM-driven matching, no
embedding similarity. The injector picks by topical_tag exact match
(or any if the caller doesn't provide one), then by priority, then by
age. Smarter selection is a follow-up if signal warrants it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, Index, String, Text

from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base


class PendingQuestion(Base):
    __tablename__ = "pending_question"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # The question text Emi will append to a reply, verbatim. Phrase as
    # a real question, not a sentence ("did the dog walk happen?", not
    # "the dog walk").
    question_text = Column(Text, nullable=False)

    # Topical tag for contextual matching. Free string; common values:
    # 'meals', 'family', 'work', 'wellness', 'romantic', 'system',
    # 'kg_maintenance', 'general'. The injector prefers tags matching
    # the current conversation's topic; if no match, falls back to the
    # oldest highest-priority pending question regardless of tag.
    topical_tag = Column(String(64), nullable=False, default="general", index=True)

    # 'low' | 'medium' | 'high'. High questions get asked first within
    # their topic, and can fire even when the injector is otherwise
    # budget-constrained (one slot per day reserved for high).
    priority = Column(String(16), nullable=False, default="medium", index=True)

    # 'pending' | 'asked' | 'answered' | 'closed' | 'dismissed' | 'expired'.
    status = Column(String(16), nullable=False, default="pending", index=True)

    # Concern in the subconscious register this question serves, when the
    # noticer asked it. The captured answer routes back to this concern.
    related_concern_id = Column(String, nullable=True, index=True)

    # 'chat' (woven naturally into a reply — default) | 'ticket' (blocking
    # modal; reserved for high-stakes questions with closing windows).
    ask_mode = Column(String(16), nullable=True)

    # Answer capture (per-turn check or hourly sweeper).
    answered_at = Column(AwareUtcDateTime, nullable=True)
    answer_text = Column(Text, nullable=True)
    answer_message_id = Column(String, nullable=True)

    # Free string naming the agent or pipeline that enqueued the
    # question, for audit / dedup. e.g. 'subconscious::noticer',
    # 'dayflow_orchestrator', 'kg_investigator', 'manual'.
    created_by = Column(String(128), nullable=True)

    # When the question becomes stale and the injector should skip it
    # rather than ask. Null = no expiration (uncommon — most questions
    # expire after a day or three).
    expires_at = Column(AwareUtcDateTime, nullable=True, index=True)

    # When the injector picked this up and appended it to an outbound
    # reply. Null while pending.
    asked_at = Column(AwareUtcDateTime, nullable=True, index=True)

    # Optional reference to the outbound chat message the question was
    # appended to, for traceability.
    asked_in_message_id = Column(String, nullable=True)

    # Free string for dismissal/expiration reason. Audit-friendly.
    dismissed_reason = Column(Text, nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(
        AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now,
    )

    __table_args__ = (
        Index("ix_pending_question_status_tag", "status", "topical_tag"),
    )
