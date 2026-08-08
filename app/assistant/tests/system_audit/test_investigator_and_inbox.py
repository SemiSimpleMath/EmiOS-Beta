"""Investigator runner (stubbed agent) + resolution ingest round-trip."""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from types import SimpleNamespace

import pytest

from app.assistant.system_audit import case_store as cs
from app.assistant.system_audit import evidence, inbox, investigator_runner as ir
from app.assistant.utils.time_utils import utc_now


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    from app.models.base import Base, get_current_engine, get_session
    from app.assistant.database.system_audit_case import SystemAuditCase
    Base.metadata.create_all(get_current_engine(), checkfirst=True)
    s = get_session()
    try:
        s.query(SystemAuditCase).delete()
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(evidence, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "INBOX_DIR", tmp_path / "inbox")
    yield


def _assembled_case(tmp_path) -> str:
    cid = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                       bound_ids={"message_ids": ["ul_ir_1"]},
                       summary="fixture", anchor_at=utc_now(),
                       quote={"quote": "wrong", "message_id": "ul_ir_1", "kind": "wrong_behavior"})
    evidence.assemble(cid, log_path=str(tmp_path / "none.log"))
    return cid


def _stub_agent(monkeypatch, data):
    from app.assistant.ServiceLocator.service_locator import DI
    stub = SimpleNamespace(action_handler=lambda msg: SimpleNamespace(data=data))
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: stub)


def test_investigation_moves_case_to_awaiting_and_appends_read(tmp_path, monkeypatch):
    cid = _assembled_case(tmp_path)
    _stub_agent(monkeypatch, {
        "summary": "double ask", "causal_chain": "A -> B", "implicated_subsystem": "Tickets",
        "repair_options": [{"level": "code", "description": "fix the join"}],
        "confidence": 0.8, "needs_claude": True,
    })
    assert ir.run_investigations() == 1
    row = [c for c in cs.list_cases() if c["id"] == cid][0]
    assert row["status"] == "awaiting_claude"
    assert row["implicated_subsystem"] == "tickets"
    text = open(row["dossier_path"], encoding="utf-8").read()
    assert "status: awaiting_claude" in text
    assert "What I need from Claude" in text and "fix the join" in text


def test_regression_is_flagged_in_dossier(tmp_path, monkeypatch):
    a = _assembled_case(tmp_path)
    _stub_agent(monkeypatch, {"summary": "s", "causal_chain": "c",
                              "implicated_subsystem": "dispatch",
                              "repair_options": [], "confidence": 0.9, "needs_claude": True})
    ir.run_investigations()
    cs.transition(a, "resolved", resolution={"commits": ["c1"]})

    b = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                     bound_ids={"message_ids": ["ul_ir_2"]}, summary="again",
                     anchor_at=utc_now())
    evidence.assemble(b, log_path=str(tmp_path / "none.log"))
    ir.run_investigations()
    row = [c for c in cs.list_cases() if c["id"] == b][0]
    assert row["status"] == "awaiting_claude" and row["recurrence_of"] == a
    assert "REGRESSION" in open(row["dossier_path"], encoding="utf-8").read()


def test_resolution_ingest_round_trip(tmp_path, monkeypatch):
    cid = _assembled_case(tmp_path)
    _stub_agent(monkeypatch, {"summary": "s", "causal_chain": "c",
                              "implicated_subsystem": "scope",
                              "repair_options": [], "confidence": 0.7, "needs_claude": True})
    ir.run_investigations()
    row = [c for c in cs.list_cases() if c["id"] == cid][0]
    path = row["dossier_path"]
    text = open(path, encoding="utf-8").read()
    text = text.replace("status: awaiting_claude", "status: resolved", 1)
    text += "\n\n## Resolution\n\nFixed the join. Commit deadbeefcafe1234.\n"
    open(path, "w", encoding="utf-8").write(text)

    assert inbox.ingest() == 1
    row = cs.list_cases(statuses=["resolved"])[0]
    assert row["id"] == cid
    cases = {c["id"]: c for c in cs.list_cases(statuses=["resolved"])}
    assert "deadbeefcafe1234" in str(cases[cid])  # commit harvested into resolution


def test_ingest_ignores_untouched_files(tmp_path, monkeypatch):
    cid = _assembled_case(tmp_path)
    _stub_agent(monkeypatch, {"summary": "s", "causal_chain": "c",
                              "implicated_subsystem": "kg",
                              "repair_options": [], "confidence": 0.7, "needs_claude": True})
    ir.run_investigations()
    assert inbox.ingest() == 0
    assert cs.list_cases(statuses=["awaiting_claude"])[0]["id"] == cid
