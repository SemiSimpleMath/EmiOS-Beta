"""Ask anchors — every surfaced question must carry the row id it rode with.

The gap (found 2026-07-08 while fixing the subconscious audit): both delivery
bridges called mark_asked WITHOUT asked_in_message_id, and answer capture
skips anchor-less questions ("no ask anchor; leaving for expiry") — so the
ask→answer capture loop was dead for every chat-nudged and starter-surfaced
question; only ticket-mode questions round-tripped.

Pinned here:
- the injector threads asked_in_message_id through to mark_asked;
- set_ask_anchor late-anchors an asked question (proactive surfaces only know
  their outbound row id after emitting) — first anchor wins, non-asked rows
  refuse;
- the digest marks the questions it rendered as asked, anchored to the digest
  row, and a quiet-day digest still renders them (mark == shown).
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_ask_anchor")

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401  (register table with Base)
from app.models.base import Base, get_session

from app.assistant.database.pending_question import PendingQuestion
from app.assistant.pending_questions import (
    enqueue_question,
    mark_asked,
    pick_question_for_nudge,
    set_ask_anchor,
)


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


# ---------------------------------------------------------------------------
# injector threads the anchor
# ---------------------------------------------------------------------------

def test_pick_for_nudge_stamps_anchor():
    qid = enqueue_question(question_text="Any plans this weekend?", created_by="test")
    picked = pick_question_for_nudge(asked_in_message_id="row-inbound-1")
    assert picked is not None and picked[0] == qid
    row = _get(qid)
    assert row.status == "asked"
    assert row.asked_in_message_id == "row-inbound-1"


def test_pick_for_nudge_without_anchor_still_asks():
    qid = enqueue_question(question_text="Q?", created_by="test")
    picked = pick_question_for_nudge()
    assert picked is not None and picked[0] == qid
    assert _get(qid).asked_in_message_id is None


# ---------------------------------------------------------------------------
# late anchoring (proactive surfaces)
# ---------------------------------------------------------------------------

def test_set_ask_anchor_fills_empty_anchor_only():
    qid = enqueue_question(question_text="Q?", created_by="test")
    # Not asked yet → refuse.
    assert set_ask_anchor(qid, asked_in_message_id="row-x") is False
    assert mark_asked(qid)
    assert set_ask_anchor(qid, asked_in_message_id="row-outbound-1") is True
    assert _get(qid).asked_in_message_id == "row-outbound-1"
    # First anchor wins.
    assert set_ask_anchor(qid, asked_in_message_id="row-other") is False
    assert _get(qid).asked_in_message_id == "row-outbound-1"


def test_set_ask_anchor_rejects_blank():
    qid = enqueue_question(question_text="Q?", created_by="test")
    mark_asked(qid)
    assert set_ask_anchor(qid, asked_in_message_id="  ") is False


# ---------------------------------------------------------------------------
# the digest bridge: rendered == marked, anchored to the digest row
# ---------------------------------------------------------------------------

def test_digest_marks_rendered_questions_with_anchor(monkeypatch, tmp_path):
    from app.assistant.subconscious import digest_runner

    qid = enqueue_question(question_text="Want me to book the dentist?", created_by="subconscious::noticer")

    monkeypatch.setattr(digest_runner, "load_register", lambda: {
        "active": [], "addressing": [], "resolved": [], "dormant": [],
    })
    monkeypatch.setattr(digest_runner, "load_digest_state", lambda: {
        "last_digest_at_utc": None, "previously_surfaced_concern_ids": [], "history_count": 0,
    })
    saved_state = {}
    monkeypatch.setattr(digest_runner, "save_digest_state", lambda s: saved_state.update(s))
    monkeypatch.setattr(
        digest_runner, "_persist_digest_to_unified_log",
        lambda **kw: "digest-row-42",
    )

    summary = digest_runner.run_digest_pass(post=True, write_file=False)

    assert summary["questions_anchored"] == 1
    row = _get(qid)
    assert row.status == "asked"
    assert row.asked_in_message_id == "digest-row-42"


def test_quiet_day_digest_still_renders_questions():
    """The runner marks what it loaded — so the renderer must show it even on
    a quiet day, or the user gets 'asked' something they never saw."""
    from app.assistant.subconscious.digest_builder import render_digest

    text = render_digest(
        register={"active": [], "addressing": [], "resolved": [], "dormant": []},
        previously_surfaced_ids=set(),
        pending_questions=[{"id": "q1", "text": "Coffee stock is low — reorder?"}],
    )
    assert "Quiet day" in text
    assert "Coffee stock is low" in text


def test_digest_preview_consumes_nothing(monkeypatch):
    from app.assistant.subconscious import digest_runner

    qid = enqueue_question(question_text="Q?", created_by="subconscious::noticer")
    monkeypatch.setattr(digest_runner, "load_register", lambda: {
        "active": [], "addressing": [], "resolved": [], "dormant": [],
    })
    monkeypatch.setattr(digest_runner, "load_digest_state", lambda: {
        "last_digest_at_utc": None, "previously_surfaced_concern_ids": [], "history_count": 0,
    })
    monkeypatch.setattr(digest_runner, "save_digest_state", lambda s: None)

    summary = digest_runner.run_digest_pass(post=False, write_file=False)
    assert summary["questions_anchored"] == 0
    assert _get(qid).status == "pending"
