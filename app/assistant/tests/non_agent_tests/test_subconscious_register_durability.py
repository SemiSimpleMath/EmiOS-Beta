"""Register durability + run serialization (2026-07-07 subconscious audit).

The concerns register is the subconscious's spine. Pinned here:
- an UNREADABLE concern row raises instead of silently starting fresh (the old
  behavior meant the next save destroyed every concern);
- an EMPTY concerns table still bootstraps (first run);
- answer capture's concern journaling lives in persist (one lock, one
  transaction) and journals onto the right concern;
- a second noticer tick started while one is in flight SKIPS instead of
  running concurrently against the same register;
- the arbiter's single product (plan.weekly_schedule) fails LOUD on a mint
  error instead of reporting ok with nothing persisted;
- the injector's ask budget counts by asked_at across statuses — an answered
  question still spent its slot.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_subconscious_register_durability")

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401  (register table with Base)
from app.models.base import Base, get_session

from app.assistant.database.pending_question import PendingQuestion
from app.assistant.pending_questions import enqueue_question, mark_answered, mark_asked
from app.assistant.pending_questions.store import count_asked_in_window
from app.assistant.subconscious import concern_store
from app.assistant.subconscious.persist import annotate_concern_answer, apply_noticer_output
from app.assistant.tests.concern_store_helpers import ScratchRegister


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


def _store(tmp_path, register=None) -> ScratchRegister:
    store = ScratchRegister(tmp_path)
    return store.write(register) if register is not None else store


def _corrupt_row(store: ScratchRegister) -> None:
    """A concern row whose record is not JSON — the table's version of a corrupt file."""
    concern_store.ensure_schema(store.connect)
    with store.connect(True) as c:
        c.execute("INSERT INTO concerns (concern_id, status, position, title, updated_at, data) "
                  "VALUES ('c-bad', 'active', 0, 't', 'now', '{ this is not json !!')")


# ---------------------------------------------------------------------------
# unreadable / empty register
# ---------------------------------------------------------------------------

def test_corrupt_register_raises_and_is_not_overwritten(tmp_path):
    store = _store(tmp_path)
    _corrupt_row(store)
    with pytest.raises(json.JSONDecodeError):
        apply_noticer_output(
            {"new_concerns": [{"label": "N1", "title": "t", "done_when": "d"}]},
            connect=store.connect, tick_log_path=store.tick_log,
        )
    # The unreadable record is still there for a human to recover — nothing wiped.
    with store.connect(False) as c:
        assert c.execute("SELECT data FROM concerns WHERE concern_id='c-bad'").fetchone()[0].startswith("{ this is not")
        assert c.execute("SELECT COUNT(*) FROM concerns").fetchone()[0] == 1


def test_missing_register_bootstraps_empty(tmp_path):
    store = _store(tmp_path)
    summary = apply_noticer_output(
        {"new_concerns": [{"label": "N1", "title": "fresh start", "done_when": "d"}]},
        connect=store.connect, tick_log_path=store.tick_log,
    )
    assert summary["new_concerns_count"] == 1
    assert [c["title"] for c in store.read()["active"]] == ["fresh start"]


# ---------------------------------------------------------------------------
# answer journaling lives in persist (one lock, one transaction)
# ---------------------------------------------------------------------------

def test_annotate_concern_answer_journals(tmp_path):
    store = _store(tmp_path, {"schema_version": 1, "active": [
        {"concern_id": "c-a", "title": "t", "reinforcement_notes": ""},
    ], "addressing": [], "resolved": [], "dormant": []})

    assert annotate_concern_answer("c-a", question_text="Q?", answer_text="A!", connect=store.connect) is True
    assert "USER ANSWERED (Q?): A!" in store.read()["active"][0]["reinforcement_notes"]

    # Unknown concern → False, register untouched.
    assert annotate_concern_answer("nope", question_text="Q?", answer_text="A!", connect=store.connect) is False


def test_annotate_on_corrupt_register_raises(tmp_path):
    store = _store(tmp_path)
    _corrupt_row(store)
    with pytest.raises(json.JSONDecodeError):
        annotate_concern_answer("c-a", question_text="Q?", answer_text="A!", connect=store.connect)


# ---------------------------------------------------------------------------
# one noticer tick at a time
# ---------------------------------------------------------------------------

def test_noticer_run_skips_when_one_is_in_flight():
    from app.assistant.routine_handlers import subconscious as handlers

    assert handlers._NOTICER_RUN_LOCK.acquire(blocking=False)
    try:
        out = handlers.noticer_run()
        assert out == {"status": "skipped_concurrent_run"}
    finally:
        handlers._NOTICER_RUN_LOCK.release()


# ---------------------------------------------------------------------------
# arbiter's single product fails loud
# ---------------------------------------------------------------------------

def test_arbiter_schedule_pod_mint_failure_raises(monkeypatch):
    from app.assistant.subconscious import scheduler_arbiter_persist as sap

    class _BoomStore:
        def put(self, pod):
            raise RuntimeError("simulated pod store failure")

    monkeypatch.setattr(sap, "PodStore", lambda: _BoomStore())
    with pytest.raises(RuntimeError, match="simulated pod store failure"):
        sap.apply_scheduler_arbiter_output({
            "week_start_date": "2026-07-13",
            "weekly_schedule": [{"date": "2026-07-13", "summary": "x", "domain": "meal"}],
        })


# ---------------------------------------------------------------------------
# ask budget counts spent slots regardless of status
# ---------------------------------------------------------------------------

def test_budget_counts_answered_questions():
    qid = enqueue_question(question_text="How was dinner?", created_by="test")
    assert mark_asked(qid, asked_in_message_id="msg-1")
    assert count_asked_in_window(hours=24.0) == 1
    # Answering must NOT free the budget slot.
    assert mark_answered(qid, answer_text="Great", answer_message_id="msg-2")
    assert count_asked_in_window(hours=24.0) == 1
