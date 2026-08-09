"""Friction ear: filtering, thresholding, case opening (classifier stubbed)."""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from types import SimpleNamespace

import pytest

from app.assistant.system_audit.signal_service import AuditSignalService
from app.assistant.system_audit import case_store as cs


def _envelope(room="master_room", role="user", content="why am I being asked this again?",
              sid="ul_123", at="2026-08-08T18:00:00+00:00"):
    return SimpleNamespace(
        signal_id=sid, source_type="unified_log", source_id="user",
        occurred_at_utc=at, signal_type="chat", content=content, data={},
        metadata={"room_id": room, "speaker_role": role, "speaker_name": "User"},
    )


@pytest.fixture(autouse=True)
def clean_cases():
    from app.models.base import Base, get_current_engine, get_session
    from app.assistant.database.system_audit_case import SystemAuditCase
    Base.metadata.create_all(get_current_engine(), checkfirst=True)
    s = get_session()
    try:
        s.query(SystemAuditCase).delete()
        s.commit()
    finally:
        s.close()
    yield


def _service(monkeypatch, verdict):
    svc = AuditSignalService()
    monkeypatch.setattr(svc, "_classify", lambda text: verdict)
    return svc


class TestFiltering:
    def test_assistant_messages_ignored(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": True, "confidence": 0.9, "quote": "x", "kind": "other"})
        svc.handle_envelope(_envelope(role="assistant"))
        assert cs.list_cases() == []

    def test_unwatched_room_ignored(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": True, "confidence": 0.9, "quote": "x", "kind": "other"})
        svc.handle_envelope(_envelope(room="telegram/123"))
        assert cs.list_cases() == []

    def test_slack_rooms_are_watched(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": True, "confidence": 0.9,
                                     "quote": "this is wrong", "kind": "wrong_behavior"})
        svc.handle_envelope(_envelope(room="slack/C123"))
        rows = cs.list_cases()
        assert len(rows) == 1 and rows[0]["room_id"] == "slack/C123"


class TestThreshold:
    def test_low_confidence_ignored(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": True, "confidence": 0.4, "quote": "hm", "kind": "other"})
        svc.handle_envelope(_envelope())
        assert cs.list_cases() == []

    def test_no_friction_ignored(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": False, "confidence": 0.95, "quote": "", "kind": "other"})
        svc.handle_envelope(_envelope())
        assert cs.list_cases() == []


class TestCaseOpening:
    def test_friction_opens_id_bound_case(self, monkeypatch):
        svc = _service(monkeypatch, {"friction": True, "confidence": 0.85,
                                     "quote": "why am I being asked this again?",
                                     "kind": "repeat_ask"})
        svc.handle_envelope(_envelope())
        rows = cs.list_cases()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "open" and row["trigger_kind"] == "user_friction"
        assert row["bound_ids"]["message_ids"] == ["ul_123"]
        assert row["friction_quotes"][0]["kind"] == "repeat_ask"

    def test_same_message_id_attaches_not_twins(self, monkeypatch):
        v = {"friction": True, "confidence": 0.9, "quote": "wrong!", "kind": "wrong_behavior"}
        svc = _service(monkeypatch, v)
        svc.handle_envelope(_envelope(sid="ul_9"))
        svc.handle_envelope(_envelope(sid="ul_9"))
        rows = cs.list_cases()
        assert len(rows) == 1 and len(rows[0]["friction_quotes"]) == 2

    def test_classifier_crash_is_contained(self, monkeypatch):
        svc = AuditSignalService()
        def boom(text):
            raise RuntimeError("llm down")
        monkeypatch.setattr(svc, "_classify", boom)
        svc.handle_envelope(_envelope())   # must not raise (gut contract)
        assert cs.list_cases() == []


class TestAuditorFindingsBridge:
    def test_finding_with_ids_opens_bucketed_case(self):
        from app.assistant.pipelines.dayflow.utils.situation_audit_runner import _findings_to_cases
        _findings_to_cases([{
            "category": "conflict", "summary": "planner re-created declined work",
            "details": "user declined; evaluator re-minted", "severity": "medium",
            "related_ids": ["work_abc123def456", "17"],
        }])
        rows = cs.list_cases()
        assert len(rows) == 1
        row = rows[0]
        assert row["trigger_kind"] == "auditor_finding"
        assert row["bound_ids"]["work_ids"] == ["work_abc123def456"]
        assert row["bound_ids"]["item_ids"] == ["17"]
        assert "planner re-created declined work" in row["summary"]

    def test_finding_without_ids_is_not_cased(self):
        from app.assistant.pipelines.dayflow.utils.situation_audit_runner import _findings_to_cases
        _findings_to_cases([{"category": "anomaly", "summary": "calendar feels dense",
                             "details": "", "severity": "medium", "related_ids": []}])
        assert cs.list_cases() == []

    def test_repeat_finding_attaches_by_id_join(self):
        from app.assistant.pipelines.dayflow.utils.situation_audit_runner import _findings_to_cases
        f = {"category": "conflict", "summary": "same conflict", "details": "",
             "severity": "high", "related_ids": ["work_repeat01"]}
        _findings_to_cases([f])
        _findings_to_cases([f])   # next hourly pass
        assert len(cs.list_cases()) == 1
