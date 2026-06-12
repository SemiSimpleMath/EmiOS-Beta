"""The wiki synthetic-fact drain (2026-06-11).

synthetic_fact_proposal findings used to dead-end in a review queue.
The drain gates them (trivia dismissed), turns worthy ones into
confirmation questions, and on a confirmed answer hands the finding to
the existing executor (status=investigated, disposition=auto_apply).
LLM agents are stubbed; this exercises the deterministic plumbing.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_wiki_fact_drain")

import uuid

import pytest

import app.assistant.tests.test_setup  # noqa: F401

import app.assistant.database.pending_question  # noqa: F401
import app.assistant.database.kg_maintenance_finding  # noqa: F401
from app.models.base import Base, get_session

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.pending_question import PendingQuestion
from app.assistant.pending_questions import get_by_creator, mark_answered, mark_asked
from app.assistant.wiki_generator import synthetic_fact_drain as drain


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


def _mk_proposal(claim: str, *, page: str = "SomePage", confidence: float = 0.9) -> str:
    fid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(KGMaintenanceFinding(
            id=fid,
            finding_type="synthetic_fact_proposal",
            status="pending",
            priority="medium",
            primary_node_id=str(uuid.uuid4()),
            suggested_action="review",
            reason=f"Inferred from wiki page on '{page}': {claim} (conf={confidence})",
            confidence=confidence,
            agent_name="wiki_connection_investigator",
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


def _stub_agents(monkeypatch, responses: dict):
    """responses: agent_name -> callable(agent_input) -> dict"""
    def _fake_run(agent_name, agent_input):
        return responses[agent_name](agent_input)
    monkeypatch.setattr(drain, "_run_agent", _fake_run)


def test_claim_extraction_from_reason():
    reason = "Inferred from wiki page on 'Alex': Alex had a company in Irvine. (conf=0.98)"
    assert drain._claim_from_reason(reason) == "Alex had a company in Irvine."
    assert drain._source_page_from_reason(reason) == "Alex"


def test_trivia_dismissed_worthy_becomes_question(monkeypatch):
    trivia = _mk_proposal("Water is a drink and a resource.", confidence=0.96)
    worthy = _mk_proposal("Alex had a company in Irvine.", page="Alex", confidence=0.98)

    def question_writer(agent_input):
        if "Water" in agent_input["claim"]:
            return {"worthy": False, "skip_reason": "taxonomy trivia"}
        return {
            "worthy": True,
            "question_text": "Am I right that Alex had a company in Irvine?",
            "if_unanswered": "leave the graph unchanged",
        }

    _stub_agents(monkeypatch, {"wiki::fact_question_writer": question_writer})
    summary = drain.drain_pending_proposals()

    assert summary["trivia_dismissed"] == 1
    assert summary["questions_asked"] == 1
    assert _finding(trivia).status == "dismissed"
    assert "trivia gate" in _finding(trivia).execution_notes

    worthy_row = _finding(worthy)
    assert worthy_row.status == "pending"
    assert worthy_row.investigation_report_json["drain_stage"] == "question_asked"

    questions = get_by_creator(created_by=drain.CREATED_BY, status="pending")
    assert len(questions) == 1
    q = questions[0]
    assert q.related_concern_id == f"finding:{worthy}"
    assert "if no reply" in q.question_text

    # Second pass: the asked finding is skipped (no double-ask).
    summary2 = drain.drain_pending_proposals()
    assert summary2["questions_asked"] == 0


def test_confirmed_answer_promotes_finding_for_executor(monkeypatch):
    fid = _mk_proposal("Alex had a company in Irvine.", page="Alex")
    _stub_agents(monkeypatch, {
        "wiki::fact_question_writer": lambda ai: {
            "worthy": True, "question_text": "Am I right that Alex had a company in Irvine?",
            "if_unanswered": "leave it",
        },
        "wiki::fact_answer_judge": lambda ai: {
            "verdict": "confirmed", "notes": "clear yes",
        },
    })
    drain.drain_pending_proposals()
    q = get_by_creator(created_by=drain.CREATED_BY, status="pending")[0]
    mark_asked(q.id, asked_in_message_id="msg-1")
    mark_answered(q.id, answer_text="Yes that's right", answer_message_id="msg-2")

    summary = drain.process_drain_answers()
    assert summary["confirmed"] == 1

    f = _finding(fid)
    assert f.status == "investigated"
    assert f.investigated_at is not None
    report = f.investigation_report_json
    assert report["disposition"] == "auto_apply"
    assert "Alex had a company in Irvine." in report["recommendation"]
    assert "Yes that's right" in report["recommendation"]
    assert get_by_creator(created_by=drain.CREATED_BY, status="closed")[0].id == q.id


def test_denied_answer_dismisses_finding(monkeypatch):
    fid = _mk_proposal("The household has a boat.", page="Household")
    _stub_agents(monkeypatch, {
        "wiki::fact_question_writer": lambda ai: {
            "worthy": True, "question_text": "Do you have a boat?", "if_unanswered": "leave it",
        },
        "wiki::fact_answer_judge": lambda ai: {"verdict": "denied", "notes": "clear no"},
    })
    drain.drain_pending_proposals()
    q = get_by_creator(created_by=drain.CREATED_BY, status="pending")[0]
    mark_asked(q.id)
    mark_answered(q.id, answer_text="No, we don't")

    summary = drain.process_drain_answers()
    assert summary["denied"] == 1
    f = _finding(fid)
    assert f.status == "dismissed"
    assert "user denied" in f.execution_notes


def test_corrected_answer_uses_corrected_claim(monkeypatch):
    fid = _mk_proposal("Alex had a company in Irvine.", page="Alex")
    _stub_agents(monkeypatch, {
        "wiki::fact_question_writer": lambda ai: {
            "worthy": True, "question_text": "Company in Irvine?", "if_unanswered": "leave it",
        },
        "wiki::fact_answer_judge": lambda ai: {
            "verdict": "corrected",
            "corrected_claim": "Alex had a company in Tustin, not Irvine.",
            "notes": "location fixed",
        },
    })
    drain.drain_pending_proposals()
    q = get_by_creator(created_by=drain.CREATED_BY, status="pending")[0]
    mark_asked(q.id)
    mark_answered(q.id, answer_text="Close - it was in Tustin")

    drain.process_drain_answers()
    report = _finding(fid).investigation_report_json
    assert "Tustin" in report["recommendation"]


def test_expired_question_parks_finding(monkeypatch):
    from datetime import timedelta
    from app.assistant.utils.time_utils import utc_now

    fid = _mk_proposal("Something optional.", page="X")
    _stub_agents(monkeypatch, {
        "wiki::fact_question_writer": lambda ai: {
            "worthy": True, "question_text": "Confirm?", "if_unanswered": "leave it",
        },
    })
    drain.drain_pending_proposals()
    q = get_by_creator(created_by=drain.CREATED_BY, status="pending")[0]
    mark_asked(q.id)
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter_by(id=q.id).one()
        row.asked_at = utc_now() - timedelta(hours=drain.QUESTION_EXPIRY_HOURS + 1)
        session.commit()
    finally:
        session.close()

    expired = drain.expire_stale_drain_questions()
    assert expired == 1
    f = _finding(fid)
    assert f.status == "pending"
    assert f.investigation_report_json["drain_stage"] == "question_expired"
