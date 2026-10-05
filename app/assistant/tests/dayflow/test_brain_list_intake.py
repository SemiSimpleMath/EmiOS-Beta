"""Work from the brain's list: the list-item intake node (docs/design/brain_delegates_work_2026-10-04.md, section 3).

Pins: with every lane off nothing happens; in a live lane an entry decided needed gets a work object shell built by
code (objective = the outcome and when, the brief, the entry id and its belief or concern in the constraints) and
each change is acted on once; a changed entry with active work is revised and its re-plan licensed; an entry
decided not needed, or cancelled, sends an end instruction with the brain's reason; an entry changed after its work
ended is recorded only; an entry before the lane's start is left to the steward; a dry run makes nothing and leaves
the changes for the live run. Scratch list and deliveries; the dayflow test store; the brief is a fake.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.dayflow_orchestrator import brain_list_intake as B
from app.assistant.subconscious import brain_list as L
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
DAY = "2026-03-10"
LIVE = {"calendar": {"mode": "live", "from_day": DAY}, "concern": {"mode": "off", "since": None}}
OFF = {"calendar": {"mode": "off", "from_day": None}, "concern": {"mode": "off", "since": None}}


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "brief", lambda entry: f"BRIEF of {entry['id']}")
    return sqlite_file(tmp_path / "list.db"), sqlite_file(tmp_path / "deliveries.db")


def _decide(lst, decision="needed", when="9:00 PM", day=DAY, at=NOW, belief="B3"):
    item = {"label": "I1", "belief_key": belief, "times": ["9:00 PM"], "statement": "The owner wants the AC at 70."}
    d = {"item": "I1", "decision": decision, "assistant_part": "set the AC to 70" if decision == "needed" else "",
         "when": when if decision == "needed" else "", "covers": [], "why": "A standing wish.", "new_since": []}
    return L.apply_calendar(day, [item], [d], placed_ids=[L.calendar_id(day, belief)], cause="test", at=at,
                            connect=lst)


def _deliver(scratch, config=LIVE, at=NOW):
    lst, dl = scratch
    return B.deliver(_store(), now_utc=at, connect=dl, list_connect=lst, lane_config=config)


def test_with_every_lane_off_nothing_happens(scratch):
    _decide(scratch[0])
    assert _deliver(scratch, OFF) == {"created": [], "revised": [], "ended": [], "recorded": [], "dry_run": [],
                                      "licensed": []}
    assert _store().list_work_objects() == []


def test_a_needed_entry_becomes_a_shell_once(scratch):
    _decide(scratch[0])
    out = _deliver(scratch)
    [work_id] = out["created"]
    wo = _store().load(work_id)
    c = wo.constraints
    assert c["list_entry_id"] == L.calendar_id(DAY, "B3") and c["belief_refs"] == ["B3"] and c["concern_refs"] == []
    assert c["objective"] == "set the AC to 70 (when: 9:00 PM)" and c["rationale"] == f"BRIEF of {c['list_entry_id']}"
    assert wo.status == "active" and not any(wo.is_work_unit(n) for n in wo.nodes.values()), "the architect plans it"
    assert _deliver(scratch)["created"] == [], "a change is acted on once"


def test_a_changed_entry_revises_its_work_and_licenses_the_replan(scratch):
    _decide(scratch[0])
    [work_id] = _deliver(scratch)["created"]
    _decide(scratch[0], when="8:30 PM", at=NOW + timedelta(minutes=5))
    out = _deliver(scratch, at=NOW + timedelta(minutes=6))
    assert out["revised"] == [work_id] and out["licensed"] == [work_id]
    assert _store().load(work_id).constraints["objective"] == "set the AC to 70 (when: 8:30 PM)"


def test_not_needed_or_cancelled_ends_the_work_with_the_brains_reason(scratch):
    lst, _ = scratch
    _decide(lst)
    [work_id] = _deliver(scratch)["created"]
    _decide(lst, decision="not_needed", at=NOW + timedelta(minutes=5))
    assert _deliver(scratch)["ended"] == [work_id]
    [instruction] = _store().load(work_id).constraints["architect_instructions"]
    assert instruction["reason"] == "The brain: A standing wish." and instruction["from"] == "brain"
    _decide(lst, belief="B4")
    [other] = _deliver(scratch)["created"]
    L.apply_calendar(DAY, [], [], placed_ids=[L.calendar_id(DAY, "B3")], cause="dropped", at=NOW, connect=lst)
    assert _deliver(scratch)["ended"] == [other], "a placement dropped from the plan cancels its entry and its work"


def test_a_change_after_the_work_ended_is_recorded_only(scratch):
    lst, _ = scratch
    _decide(lst)
    [work_id] = _deliver(scratch)["created"]
    store = _store()
    store.apply("set_work_status", {"work_id": work_id, "status": "done", "reason": "set at 9 PM"}, actor="test")
    _decide(lst, when="8:30 PM", at=NOW + timedelta(minutes=5))
    out = _deliver(scratch)
    assert out["recorded"] == [L.calendar_id(DAY, "B3")] and out["created"] == [] and out["revised"] == []


def test_a_day_before_the_lane_starts_stays_the_stewards(scratch):
    _decide(scratch[0], day="2026-03-09")
    assert _deliver(scratch)["created"] == []
    assert B.calendar_live(DAY, LIVE) and not B.calendar_live("2026-03-09", LIVE) and not B.calendar_live(DAY, OFF)


def test_a_dry_run_makes_nothing_and_leaves_the_changes_for_the_live_run(scratch):
    _decide(scratch[0])
    dry = {**LIVE, "calendar": {"mode": "dry_run", "from_day": DAY}}
    out = _deliver(scratch, dry)
    assert out["dry_run"] == [{"entry": L.calendar_id(DAY, "B3"), "action": "create", "work_id": None}]
    assert _store().list_work_objects() == [] and _deliver(scratch, dry)["dry_run"] == []
    assert len(_deliver(scratch, LIVE)["created"]) == 1


def test_a_concern_entry_carries_its_concern(scratch, monkeypatch):
    lst, _ = scratch
    from app.assistant.subconscious import concern_feedback
    monkeypatch.setattr(concern_feedback, "propagate_work_outcome", lambda store, work_id, outcome: None)
    L.apply_concern("c-ring", [{"entry": "new", "action": "new", "what": "The doorbell battery is recharged",
                                "assistant_part": "tell the owner and ask him to confirm", "when": "9:00 AM",
                                "why": "Ring sent a low-battery notice."}], cause="plan", at=NOW, connect=lst)
    config = {"calendar": {"mode": "off", "from_day": None}, "concern": {"mode": "live", "since": NOW.isoformat()}}
    [work_id] = _deliver(scratch, config)["created"]
    assert _store().load(work_id).constraints["concern_refs"] == ["concern:c-ring"]
