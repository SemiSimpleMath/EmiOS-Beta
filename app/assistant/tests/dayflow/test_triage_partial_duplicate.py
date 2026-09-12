"""A partly-covered artifact is ADMITTED, and the uncovered part reaches the evaluator.

2026-09-11: an email repeated an appointment date the system already tracked AND asked us
to relay it to the user. Triage saw the familiar facts, returned REJECT_DUPLICATE for the
whole artifact, and the request died with it — nobody was told, and nothing surfaced until
a reminder two weeks out. Coverage is judged per PART now: any uncovered part means ADMIT,
and `uncovered` names what remains so the evaluator acts on that and not on the rest.

Hermetic — fake blackboard, no LLM, no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.control_nodes.triage_spawn_guard_node import TriageSpawnGuardNode


class _BB:
    def __init__(self, state=None):
        self.d = dict(state or {})

    def get_state_value(self, k, default=None):
        return self.d.get(k, default)

    def update_state_value(self, k, v):
        self.d[k] = v

    def update_global_state_value(self, k, v):
        self.d[k] = v


def _item(item_id, short_id, summary):
    return {"id": item_id, "content": summary,
            "metadata": {"item_id": item_id, "short_id": short_id, "summary": summary,
                         "state": "important_open", "source_type": "email"}}


def _node(bb):
    n = TriageSpawnGuardNode.__new__(TriageSpawnGuardNode)
    n.name = "triage_spawn_guard_node"
    n.blackboard = bb
    return n


def _run(decisions, items):
    bb = _BB({"eligible_items_now": items, "artifact_decisions": decisions,
              "admitted_artifacts": [], "auto_admitted_artifacts": []})
    _node(bb).action_handler(SimpleNamespace())
    return bb


def test_partial_duplicate_is_admitted_and_carries_the_uncovered_part():
    items = [_item("email:1", "7600", "Re: the appointment — please tell the user")]
    bb = _run([{
        "artifact_id": "7600", "decision": "ADMIT",
        "reason": "date already tracked, but the relay request is not",
        "uncovered": "already covered: the appointment date. NOT covered: sender asks us to tell the user.",
    }], items)

    admitted = bb.get_state_value("admitted_artifacts")
    assert len(admitted) == 1
    meta = admitted[0]["metadata"]
    assert meta["state"] == "artifact"
    assert "NOT covered" in meta["triage_uncovered"]
    assert bb.get_state_value("rejected_artifacts") == []


def test_wholly_covered_artifact_is_still_rejected_and_suppressed():
    items = [_item("email:2", "7601", "Re: the appointment")]
    bb = _run([{"artifact_id": "7601", "decision": "REJECT_DUPLICATE",
                "reason": "every part already tracked"}], items)

    assert bb.get_state_value("admitted_artifacts") == []
    rejected = bb.get_state_value("rejected_artifacts")
    assert len(rejected) == 1
    meta = rejected[0]["metadata"]
    assert meta["state"] == "suppressed"
    assert meta["state_reason"] == "triage_reject_duplicate"
    assert "triage_uncovered" not in meta


def test_a_wholly_new_admit_carries_no_uncovered_note():
    items = [_item("email:3", "7602", "Something entirely new")]
    bb = _run([{"artifact_id": "7602", "decision": "ADMIT", "reason": "new"}], items)

    meta = bb.get_state_value("admitted_artifacts")[0]["metadata"]
    assert meta["state"] == "artifact"
    assert "triage_uncovered" not in meta


def test_blank_uncovered_is_not_written():
    items = [_item("email:4", "7603", "New thing")]
    bb = _run([{"artifact_id": "7603", "decision": "ADMIT", "reason": "new", "uncovered": "   "}], items)

    meta = bb.get_state_value("admitted_artifacts")[0]["metadata"]
    assert "triage_uncovered" not in meta
