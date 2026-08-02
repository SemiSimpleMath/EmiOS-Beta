"""Concern back-propagation + noticer rerun (2026-08-01 subconscious audit).

Work outcomes never reached the concerns register (19 AC-service re-mints, 4 after
an explicit user decline). Now: the evaluator cites concern:<prefix> in based_on ->
work_persist stores it on the work object -> every dayflow closure path calls
propagate_work_outcome -> the register journals the outcome (user words verbatim),
user-declined concerns park dormant (the projection reads only `active`, so the
evaluator pressure stops at the source) -> the noticer is re-run, cooldown-guarded.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.subconscious.concern_feedback import propagate_work_outcome
from app.assistant.subconscious.persist import apply_work_outcome

_CID = "a2a8a4b0-2d34-4c94-9d2e-f5e6f9c6e5d7"


def _register(tmp_path) -> Path:
    path = tmp_path / "register.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "last_updated_utc": None,
        "last_noticer_tick_utc": None,
        "active": [{
            "concern_id": _CID,
            "title": "AC service should be handled before summer heat",
            "severity": "high",
            "reinforcement_count": 23,
            "reinforcement_notes": "\n[2026-07-29] seasonal window arrived",
            "last_disposition_at_count": 18,
        }],
        "addressing": [], "resolved": [], "dormant": [],
    }), encoding="utf-8")
    return path


class TestApplyWorkOutcome:

    def test_user_decline_parks_dormant_with_words(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(
            f"concern:{_CID[:8]}", work_id="work_x", outcome="abandoned",
            user_words="Do not arrange this. Drop this task alltogether.",
            register_path=path)
        assert result == "user_declined"
        reg = json.loads(path.read_text(encoding="utf-8"))
        assert reg["active"] == []
        concern = reg["dormant"][0]
        assert "Do not arrange this" in concern["reinforcement_notes"]
        assert concern["user_declined_at_utc"]
        assert concern["last_disposition_at_count"] == 23   # pressure window reset

    def test_done_moves_to_addressing(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(_CID, work_id="work_x", outcome="done",
                                    register_path=path)
        assert result == "addressing"
        reg = json.loads(path.read_text(encoding="utf-8"))
        assert reg["active"] == []
        concern = reg["addressing"][0]
        assert "ADDRESSED by work_x" in concern["reinforcement_notes"]
        assert concern["addressing_since_utc"]

    def test_system_abandon_without_words_only_journals(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(_CID, work_id="work_x", outcome="abandoned",
                                    register_path=path)
        assert result == "journaled"
        reg = json.loads(path.read_text(encoding="utf-8"))
        assert len(reg["active"]) == 1      # a system drop must not silence a real concern

    def test_unknown_ref_is_unresolved(self, tmp_path):
        path = _register(tmp_path)
        assert apply_work_outcome("concern:ffffffff", work_id="work_x",
                                  outcome="done", register_path=path) == "unresolved"
        reg = json.loads(path.read_text(encoding="utf-8"))
        assert len(reg["active"]) == 1      # untouched


class TestPropagateWorkOutcome:

    @staticmethod
    def _wo(refs, reply_text=""):
        nodes = {}
        if reply_text:
            nodes["reply_1"] = SimpleNamespace(type="evidence", created_by="reply",
                                               content=reply_text)
        return SimpleNamespace(id="work_x", constraints={"concern_refs": refs}, nodes=nodes)

    def test_propagates_refs_with_user_words_and_reruns_noticer(self):
        store = SimpleNamespace(load=lambda wid: self._wo(
            [f"concern:{_CID[:8]}"], reply_text="Do not arrange this."))
        with patch("app.assistant.subconscious.persist.apply_work_outcome",
                   return_value="user_declined") as apply_mock, \
             patch("app.assistant.subconscious.answer_capture.trigger_noticer",
                   return_value=True) as trigger_mock:
            propagate_work_outcome(store, "work_x", "abandoned")
        apply_mock.assert_called_once_with(
            f"concern:{_CID[:8]}", work_id="work_x", outcome="abandoned",
            user_words="Do not arrange this.")
        trigger_mock.assert_called_once()

    def test_no_refs_is_a_silent_noop(self):
        store = SimpleNamespace(load=lambda wid: self._wo([]))
        with patch("app.assistant.subconscious.answer_capture.trigger_noticer") as trigger_mock:
            propagate_work_outcome(store, "work_x", "done")
        trigger_mock.assert_not_called()

    def test_failure_never_raises_into_closure(self):
        def _boom(wid):
            raise RuntimeError("store unavailable")
        propagate_work_outcome(SimpleNamespace(load=_boom), "work_x", "done")


class TestForwardEdge:

    def test_created_work_object_carries_concern_refs(self, tmp_path):
        from work_objects.store import WorkStore
        from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output
        store = WorkStore(str(tmp_path / "work.db"))
        result = persist_steward_output(store, {"new_or_changed": [{
            "work_id": "",
            "objective": "Schedule home AC service.",
            "based_on": [f"concern:{_CID[:8]}", "7128"],
        }]})
        wid = result["created"][0]["work_id"]
        wo = store.load(wid)
        assert wo.constraints["concern_refs"] == [f"concern:{_CID[:8]}"]
