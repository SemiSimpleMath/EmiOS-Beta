"""The noticer question→answer loop (2026-06-11).

Questions used to die at status='asked' — answers evaporated into chat.
Now: pending → asked → answered (captured by per-turn check / sweeper)
→ closed (noticer processed it), with concern linkage end to end.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_question_answer_loop")

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401  (register table with Base)
from app.models.base import Base, get_session

from app.assistant.database.pending_question import PendingQuestion
from app.assistant.pending_questions import (
    close_question,
    enqueue_question,
    get_asked_unanswered,
    get_for_noticer_processing,
    mark_answered,
    mark_asked,
)
from app.assistant.subconscious.persist import apply_noticer_output


@pytest.fixture(autouse=True)
def _clean_table():
    session = get_session()
    Base.metadata.create_all(session.bind)
    try:
        session.query(PendingQuestion).delete()
        session.commit()
    finally:
        session.close()
    yield


def _get(qid):
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=qid).one()
        session.expunge(row)
        return row
    finally:
        session.close()


def test_full_lifecycle_pending_asked_answered_closed():
    qid = enqueue_question(
        question_text="When was Sam's last checkup?",
        topical_tag="health",
        priority="high",
        created_by="subconscious::noticer",
        related_concern_id="concern-123",
        ask_mode="chat",
    )
    row = _get(qid)
    assert row.status == "pending"
    assert row.related_concern_id == "concern-123"
    assert row.ask_mode == "chat"

    assert mark_asked(qid, asked_in_message_id="msg-1")
    assert _get(qid).status == "asked"
    assert [q.id for q in get_asked_unanswered()] == [qid]

    assert mark_answered(qid, answer_text="It was back in April", answer_message_id="msg-2")
    row = _get(qid)
    assert row.status == "answered"
    assert row.answer_text == "It was back in April"
    assert row.answer_message_id == "msg-2"
    assert get_asked_unanswered() == []

    mailbox = get_for_noticer_processing()
    assert [q.id for q in mailbox["answered"]] == [qid]

    assert close_question(qid, outcome="closed", notes="resolved the checkup concern")
    assert _get(qid).status == "closed"


def test_mark_answered_refuses_wrong_state():
    qid = enqueue_question(question_text="Still pending?", created_by="t")
    assert mark_answered(qid, answer_text="too early") is False
    assert _get(qid).status == "pending"


def test_stale_asked_lands_in_noticer_mailbox():
    from datetime import timedelta
    from app.assistant.utils.time_utils import utc_now

    # The mailbox is ownership-filtered: only the noticer's own questions.
    qid = enqueue_question(question_text="Old ask?", created_by="subconscious::noticer")
    mark_asked(qid)
    other = enqueue_question(question_text="Someone else's ask", created_by="wiki::synthetic_fact_drain")
    mark_asked(other)
    session = get_session()
    try:
        for stale_id in (qid, other):
            row = session.query(PendingQuestion).filter_by(id=stale_id).one()
            row.asked_at = utc_now() - timedelta(hours=60)
        session.commit()
    finally:
        session.close()

    mailbox = get_for_noticer_processing(stale_after_hours=48.0)
    assert [q.id for q in mailbox["stale_asked"]] == [qid]  # the drain's question is NOT the noticer's business
    # And the capture working-set ignores it (too old to keep judging).
    assert get_asked_unanswered(max_age_hours=48.0) == []


def test_noticer_question_outcomes_close_rows(tmp_path):
    q1 = enqueue_question(question_text="Answered one", created_by="t")
    q2 = enqueue_question(question_text="Expired one", created_by="t")
    mark_asked(q1)
    mark_answered(q1, answer_text="yes")
    mark_asked(q2)

    register_path = tmp_path / "register.json"
    register_path.write_text(json.dumps({"active": [], "addressing": [], "resolved": [], "dormant": []}))
    summary = apply_noticer_output(
        {
            "question_outcomes": [
                {"question_id": q1, "outcome": "processed", "notes": "updated concern"},
                {"question_id": q2, "outcome": "expired_default_applied", "notes": "default applied"},
            ]
        },
        register_path=register_path,
        tick_log_path=tmp_path / "ticks.jsonl",
    )
    assert summary["question_outcomes_count"] == 2
    assert _get(q1).status == "closed"
    assert _get(q2).status == "expired"


def test_enqueue_derives_ticket_mode_for_high_stakes(tmp_path):
    concern = {
        "concern_id": "c-high",
        "title": "AC window closing",
        "severity": "high",
        "horizon": "this_week",
        "domain_tags": ["home"],
    }
    register_path = tmp_path / "register.json"
    register_path.write_text(json.dumps(
        {"active": [concern], "addressing": [], "resolved": [], "dormant": []}
    ))
    apply_noticer_output(
        {
            "pending_questions": [
                {
                    "question_id": "ignored",
                    "text": "Book the AC service this week?",
                    "related_concern_id": "c-high",
                    "why_asking": "window closing",
                    "if_unanswered": "book it",
                },
            ]
        },
        register_path=register_path,
        tick_log_path=tmp_path / "ticks.jsonl",
    )
    session = get_session()
    try:
        row = session.query(PendingQuestion).one()
        assert row.ask_mode == "ticket"
        assert row.related_concern_id == "c-high"
        assert row.priority == "high"
    finally:
        session.close()


def test_ticket_delivery_flips_question_to_asked(monkeypatch):
    from app.assistant.pending_questions import ticket_delivery

    qid = enqueue_question(
        question_text="Book the AC service this week?",
        priority="high",
        created_by="subconscious::noticer",
        related_concern_id="c-high",
        ask_mode="ticket",
    )
    ticket_id = ticket_delivery.deliver_question_as_ticket(
        question_id=qid,
        question_text="Book the AC service this week?",
    )
    assert ticket_id
    row = _get(qid)
    assert row.status == "asked"
    assert row.asked_in_message_id == f"ticket:{ticket_id}"

    # The ticket carries the question linkage for the response router.
    from app.assistant.ticket_manager.ticket_manager import get_ticket_manager
    ticket = get_ticket_manager().get_ticket_by_id(ticket_id)
    assert (ticket.trigger_context or {}).get("question_id") == qid


def test_ticket_response_routes_into_answer_loop(monkeypatch):
    from app.assistant.pending_questions import ticket_delivery
    from app.assistant.subconscious import answer_capture

    triggered = []
    monkeypatch.setattr(answer_capture, "trigger_noticer", lambda reason: triggered.append(reason))
    annotated = []
    monkeypatch.setattr(
        answer_capture, "annotate_concern_answer",
        lambda cid, **kw: annotated.append((cid, kw)) or True,
    )

    qid = enqueue_question(
        question_text="Book it?",
        priority="high",
        created_by="subconscious::noticer",
        related_concern_id="c-high",
        ask_mode="ticket",
    )
    ticket_id = ticket_delivery.deliver_question_as_ticket(
        question_id=qid, question_text="Book it?",
    )

    class _Msg:
        data = {"ticket_id": ticket_id, "action": "answer",
                "target_state": "accepted", "user_text": "Yes, Tuesday morning"}

    ticket_delivery._on_ticket_responded(_Msg())

    row = _get(qid)
    assert row.status == "answered"
    assert row.answer_text == "Yes, Tuesday morning"
    assert row.answer_message_id == f"ticket:{ticket_id}"
    assert annotated and annotated[0][0] == "c-high"
    assert triggered


def test_ticket_dismissal_expires_question(monkeypatch):
    from app.assistant.pending_questions import ticket_delivery

    qid = enqueue_question(
        question_text="Optional thing?",
        created_by="subconscious::noticer",
        ask_mode="ticket",
    )
    ticket_id = ticket_delivery.deliver_question_as_ticket(
        question_id=qid, question_text="Optional thing?",
    )

    class _Msg:
        data = {"ticket_id": ticket_id, "action": "dismiss",
                "target_state": "dismissed", "user_text": ""}

    ticket_delivery._on_ticket_responded(_Msg())
    assert _get(qid).status == "expired"


def test_annotate_concern_answer_journals_register(tmp_path, monkeypatch):
    from app.assistant.subconscious import answer_capture

    register = {
        "active": [{"concern_id": "c1", "title": "T", "reinforcement_notes": ""}],
        "addressing": [], "resolved": [], "dormant": [],
    }
    path = tmp_path / "resources" / "subconscious" / "resource_concerns_register.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(register))
    # The register write lives in persist now (one lock, one atomic writer);
    # patch its module-level get_repo_root binding.
    monkeypatch.setattr(
        "app.assistant.subconscious.persist.get_repo_root", lambda: tmp_path,
    )
    ok = answer_capture.annotate_concern_answer(
        "c1", question_text="When was it?", answer_text="In April",
    )
    assert ok
    saved = json.loads(path.read_text())
    assert "USER ANSWERED" in saved["active"][0]["reinforcement_notes"]
    assert "In April" in saved["active"][0]["reinforcement_notes"]
