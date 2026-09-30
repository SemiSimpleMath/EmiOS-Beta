"""Concern back-propagation (2026-08-01 subconscious audit; attached work 2026-09-30).

Work outcomes never reached the concerns register (19 AC-service re-mints, 4 after
an explicit user decline). Now: the evaluator cites concern:<prefix> in based_on ->
work_persist stores it on the work object, which is attached to the concern -> the
work's ending is recorded on the concern (user words verbatim); user-declined concerns
park dormant (the projection reads only `active`, so the evaluator pressure stops at
the source); otherwise, with no work in progress, the concern is open for the brain.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.tests.concern_store_helpers import ScratchRegister

from app.assistant.subconscious.concern_feedback import propagate_work_outcome
from app.assistant.subconscious.persist import apply_work_outcome

_CID = "a2a8a4b0-2d34-4c94-9d2e-f5e6f9c6e5d7"


def _register(tmp_path) -> Path:
    path = ScratchRegister(tmp_path)
    path.write({
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
    })
    return path


class TestApplyWorkOutcome:

    def test_user_decline_parks_dormant_with_words(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(
            f"concern:{_CID[:8]}", work_id="work_x", outcome="abandoned",
            user_response={"user_text": "No thanks", "response_details": {
                "meaning": "decline", "label": "No thanks", "typed_text": "",
                "scope": "Arrange AC service"}},
            connect=path.connect)
        assert result == "user_declined"
        reg = path.read()
        assert reg["active"] == []
        concern = reg["dormant"][0]
        assert "No thanks" in concern["reinforcement_notes"]
        assert concern["user_declined_at_utc"]
        assert concern["last_disposition_at_count"] == 23   # pressure window reset

    def test_done_records_the_ending_and_leaves_the_decision_to_the_brain(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(_CID, work_id="work_x", outcome="done",
                                    connect=path.connect)
        assert result == "ended"
        reg = path.read()
        concern = reg["active"][0]                      # done work does not settle the concern
        assert concern["attached_work"]["work_x"]["ended"]["outcome"] == "done"
        assert "WORK ENDED work_x (done)" in concern["reinforcement_notes"]

    def test_the_last_work_ending_takes_the_concern_out_of_progress(self, tmp_path):
        path = _register(tmp_path)
        reg = path.read()
        concern = reg["active"].pop()
        concern["attached_work"] = {w: {"work_id": w, "status": "active", "judgments": [], "ended": None}
                                    for w in ("work_x", "work_y")}
        reg["addressing"].append(concern)
        path.write(reg)
        apply_work_outcome(_CID, work_id="work_x", outcome="done", connect=path.connect)
        assert len(path.read()["addressing"]) == 1      # work_y is still going
        apply_work_outcome(_CID, work_id="work_y", outcome="abandoned", connect=path.connect)
        reg = path.read()
        assert reg["addressing"] == [] and len(reg["active"]) == 1

    def test_system_abandon_without_words_only_journals(self, tmp_path):
        path = _register(tmp_path)
        result = apply_work_outcome(_CID, work_id="work_x", outcome="abandoned",
                                    connect=path.connect)
        assert result == "ended"
        reg = path.read()
        assert len(reg["active"]) == 1      # a system drop must not silence a real concern

    def test_unknown_ref_is_unresolved(self, tmp_path):
        path = _register(tmp_path)
        assert apply_work_outcome("concern:ffffffff", work_id="work_x",
                                  outcome="done", connect=path.connect) == "unresolved"
        reg = path.read()
        assert len(reg["active"]) == 1      # untouched


class TestPropagateWorkOutcome:

    @staticmethod
    def _wo(refs, reply_text=""):
        nodes = {}
        if reply_text:
            nodes["reply_1"] = SimpleNamespace(type="evidence", created_by="reply",
                                               content=reply_text)
        return SimpleNamespace(id="work_x", constraints={"concern_refs": refs}, nodes=nodes)

    def test_work_citing_no_concern_leaves_no_receipt(self, tmp_path):
        from work_objects.store import WorkStore
        store = WorkStore(str(tmp_path / "work.db"))
        try:
            wo = store.apply("create_work_object", {"title": "Unlinked", "constraints": {}})
            store.apply("set_work_status", {"work_id": wo.id, "status": "done", "reason": "done"})
            assert store.pending_concern_feedback() == []
            propagate_work_outcome(store, wo.id, "done")
        finally:
            store.close()

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
