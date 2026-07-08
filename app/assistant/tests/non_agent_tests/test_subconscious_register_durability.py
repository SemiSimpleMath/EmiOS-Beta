"""Register durability + run serialization (2026-07-07 subconscious audit).

The concerns register is the subconscious's spine. Pinned here:
- a CORRUPT register raises instead of silently starting fresh (the old
  behavior meant the next save destroyed every concern);
- a MISSING register still bootstraps empty (first run);
- answer capture's concern journaling lives in persist (one lock, one atomic
  writer) and journals onto the right concern;
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
import tempfile
from pathlib import Path

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_subconscious_register_durability")

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401  (register table with Base)
from app.models.base import Base, get_session

from app.assistant.database.pending_question import PendingQuestion
from app.assistant.pending_questions import enqueue_question, mark_answered, mark_asked
from app.assistant.pending_questions.store import count_asked_in_window
from app.assistant.subconscious import persist
from app.assistant.subconscious.persist import annotate_concern_answer, apply_noticer_output


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


def _tick() -> Path:
    fd, p = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    return Path(p)


def _register_file(content: str) -> Path:
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    Path(p).write_text(content, encoding="utf-8")
    return Path(p)


# ---------------------------------------------------------------------------
# corrupt / missing register
# ---------------------------------------------------------------------------

def test_corrupt_register_raises_and_is_not_overwritten():
    reg_path = _register_file("{ this is not json !!")
    tick = _tick()
    with pytest.raises(json.JSONDecodeError):
        apply_noticer_output(
            {"new_concerns": [{"concern_id": "c-1", "title": "t"}]},
            register_path=reg_path, tick_log_path=tick,
        )
    # The corrupt bytes are still there for a human to recover — nothing wiped.
    assert reg_path.read_text(encoding="utf-8").startswith("{ this is not json")
    os.remove(reg_path); os.remove(tick)


def test_missing_register_bootstraps_empty():
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd); os.remove(p)
    reg_path, tick = Path(p), _tick()
    summary = apply_noticer_output(
        {"new_concerns": [{"concern_id": "c-1", "title": "fresh start"}]},
        register_path=reg_path, tick_log_path=tick,
    )
    assert summary["new_concerns_count"] == 1
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert [c["concern_id"] for c in reg["active"]] == ["c-1"]
    os.remove(reg_path); os.remove(tick)


# ---------------------------------------------------------------------------
# answer journaling lives in persist (one lock, one atomic writer)
# ---------------------------------------------------------------------------

def test_annotate_concern_answer_journals(monkeypatch, tmp_path):
    reg = {"schema_version": 1, "active": [
        {"concern_id": "c-a", "title": "t", "reinforcement_notes": ""},
    ], "addressing": [], "resolved": [], "dormant": []}
    reg_path = tmp_path / "resources" / "subconscious" / "resource_concerns_register.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setattr(persist, "get_repo_root", lambda: tmp_path)

    assert annotate_concern_answer("c-a", question_text="Q?", answer_text="A!") is True
    saved = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "USER ANSWERED (Q?): A!" in saved["active"][0]["reinforcement_notes"]

    # Unknown concern → False, register untouched.
    assert annotate_concern_answer("nope", question_text="Q?", answer_text="A!") is False


def test_annotate_on_corrupt_register_raises(monkeypatch, tmp_path):
    reg_path = tmp_path / "resources" / "subconscious" / "resource_concerns_register.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(persist, "get_repo_root", lambda: tmp_path)
    with pytest.raises(json.JSONDecodeError):
        annotate_concern_answer("c-a", question_text="Q?", answer_text="A!")


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
