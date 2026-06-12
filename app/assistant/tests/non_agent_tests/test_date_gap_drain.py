"""Date-gap drain: LLM gate + answer routing (2026-06-12).

Gate stubs simulate kg_maintenance::date_gap_gate; the deterministic
plumbing is what's under test: gating rejects findings, worthy ones
become linked questions, captured answers promote findings to
investigated/auto_apply for the executor.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_date_gap_drain")

import uuid

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401
import app.assistant.database.kg_maintenance_finding  # noqa: F401
from app.models.base import Base, get_session

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.pending_question import PendingQuestion
from app.assistant.kg_maintenance import date_gap_drain as drain
from app.assistant.pending_questions import get_by_creator, mark_answered, mark_asked


@pytest.fixture(autouse=True)
def _clean_tables():
    session = get_session()
    Base.metadata.create_all(session.bind)
    try:
        session.query(PendingQuestion).delete()
        session.query(KGMaintenanceFinding).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_finding(label: str, *, priority="high", entities=None) -> str:
    fid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(KGMaintenanceFinding(
            id=fid,
            finding_type="state_missing_dates",
            status="pending",
            priority=priority,
            primary_node_id=str(uuid.uuid4()),
            suggested_action="ask_user",
            reason=f"{label} has no start_date",
            evidence_json={
                "label": label,
                "node_type": "State",
                "category": "education",
                "connected_entity_labels": entities or [],
                "worth": 8.0,
            },
        ))
        session.commit()
    finally:
        session.close()
    return fid


def _finding(fid: str) -> KGMaintenanceFinding:
    session = get_session()
    try:
        row = session.query(KGMaintenanceFinding).filter_by(id=fid).one()
        session.expunge(row)
        return row
    finally:
        session.close()


def test_gate_rejects_and_asks(monkeypatch):
    chair = _mk_finding("Ownership", entities=["Swivel Chair"], priority="medium")
    school = _mk_finding("Education", entities=["UC Berkeley"])

    def gate(agent_input):
        if "Swivel Chair" in agent_input["entities"]:
            return {"worthy": False, "skip_reason": "ordinary object ownership"}
        return {"worthy": True, "question_text": "When did you graduate from UC Berkeley?"}

    monkeypatch.setattr(drain, "_run_gate", gate)
    summary = drain.run_date_gap_drain(limit=3)

    assert summary["gated_out"] == 1
    assert summary["questions_enqueued"] == 1
    assert _finding(chair).status == "rejected"
    assert "date_gap_gate" in _finding(chair).execution_notes

    questions = get_by_creator(created_by=drain.CREATED_BY, status="pending")
    assert len(questions) == 1
    q = questions[0]
    assert q.related_concern_id == f"finding:{school}"
    assert "UC Berkeley" in q.question_text
    assert "loose answer" in q.question_text

    # Cooldown: the asked finding is skipped on the next pass.
    summary2 = drain.run_date_gap_drain(limit=3)
    assert summary2["questions_enqueued"] == 0
    assert summary2["skipped_in_cooldown"] == 1


def test_answer_promotes_finding_for_executor(monkeypatch):
    school = _mk_finding("Education", entities=["UC Berkeley"])
    monkeypatch.setattr(drain, "_run_gate", lambda ai: {
        "worthy": True, "question_text": "When did you graduate from UC Berkeley?",
    })
    drain.run_date_gap_drain(limit=1)
    q = get_by_creator(created_by=drain.CREATED_BY, status="pending")[0]
    mark_asked(q.id, asked_in_message_id="msg-1")
    mark_answered(q.id, answer_text="around 2003", answer_message_id="msg-2")

    summary = drain.process_date_gap_answers()
    assert summary["promoted"] == 1

    f = _finding(school)
    assert f.status == "investigated"
    report = f.investigation_report_json
    assert report["disposition"] == "auto_apply"
    assert "around 2003" in report["recommendation"]
    assert "year-floor" in report["recommendation"]
    assert get_by_creator(created_by=drain.CREATED_BY, status="closed")[0].id == q.id


def test_legacy_question_without_linkage_is_retired():
    from app.assistant.pending_questions import enqueue_question

    qid = enqueue_question(
        question_text="Memory date gap: roughly when did the 'Ownership' state begin?",
        created_by=drain.CREATED_BY,
    )
    mark_asked(qid)
    mark_answered(qid, answer_text="no idea")

    summary = drain.process_date_gap_answers()
    assert summary["promoted"] == 0
    assert get_by_creator(created_by=drain.CREATED_BY, status="closed")[0].id == qid
