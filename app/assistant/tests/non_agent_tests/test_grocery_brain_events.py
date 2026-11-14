"""Each step of a week's grocery list reaches the brain as an event (2026-11-14), on a scratch database."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.assistant.subconscious import brain_inbox, brain_step, conversations, grocery_lists
from belief_engine.intake.store import sqlite_file

WEEK = "2026-11-16"
ITEMS = [{"name": "milk", "quantity": "1 gallon", "category": "Dairy & Eggs", "reason": "staple; none at home"}]
SKIPPED = [{"name": "cookies", "reason": "bought three weeks running"}]


@pytest.fixture
def connect(tmp_path, monkeypatch):
    c = sqlite_file(tmp_path / "grocery.db")
    monkeypatch.setattr(grocery_lists, "_connect", c)
    return c


def _events(connect):
    with connect(False) as c:
        return [dict(r) for r in c.execute("SELECT * FROM brain_events WHERE source = 'grocery' ORDER BY id")]


def test_a_planned_list_tells_the_brain_it_is_ready(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=SKIPPED, planner_note="light week", connect=connect)
    [e] = _events(connect)
    assert e["source_ref"].startswith(f"grocery:{WEEK}:ready:") and e["speaker"] == "the weekly meal planner"
    assert (e["gate_status"], e["route"], json.loads(e["concern_ids"])) == ("routed", "new_matter", [])
    assert "/meals?week=2026-11-16#grocery" in e["text"]
    assert "- milk (1 gallon): staple; none at home" in e["text"]
    assert "- cookies: bought three weeks running" in e["text"] and "light week" in e["text"]


def test_the_owners_steps_reach_the_brain(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[], connect=connect)
    grocery_lists.approve(WEEK, datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc), connect=connect)
    grocery_lists.reopen(WEEK, connect=connect)
    grocery_lists.skip(WEEK, connect=connect)
    events = _events(connect)
    assert [e["source_ref"].split(":")[2] for e in events] == ["ready", "approved", "reopened", "skipped"]
    assert all(e["speaker"] == "the owner" for e in events[1:])
    approved = events[1]["text"]
    assert "ordered from Ralphs through DoorDash at" in approved and "- milk (1 gallon)" in approved
    assert "confirms the built cart" in approved


def test_later_steps_go_to_the_concern_raised_from_that_week(connect):
    from app.assistant.subconscious import concern_store
    concern_store.save_register({"active": [
        {"concern_id": "c-thanksgiving", "title": "Prepare for Thanksgiving",
         "source": {"kind": "noticer", "ref": "noticer:2026-11-08:1"}},
        {"concern_id": "c-groceries", "title": "This week's groceries",
         "source": {"kind": "grocery", "ref": f"grocery:{WEEK}:ready:2026-11-14T22:56:06+00:00"}},
        {"concern_id": "c-last-week", "title": "Last week's groceries",
         "source": {"kind": "grocery", "ref": "grocery:2026-11-09:ready:2026-11-07T22:00:00+00:00"}},
    ]}, connect=connect)
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[], connect=connect)
    grocery_lists.approve(WEEK, datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc), connect=connect)
    for e in _events(connect):
        assert (e["gate_status"], e["route"], json.loads(e["concern_ids"])) == ("routed", "concern", ["c-groceries"])


def test_grocery_events_skip_the_gate(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[], connect=connect)
    assert [e for e in brain_inbox.pending(connect=connect) if e["source"] == "grocery"] == []


def test_a_step_lands_once(connect):
    at = datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)
    kw = dict(week_start=WEEK, change="ready", text="t", at=at, speaker="the weekly meal planner", connect=connect)
    assert brain_inbox.add_grocery_event(**kw) is True
    assert brain_inbox.add_grocery_event(**kw) is False


def test_an_unknown_step_is_refused(connect):
    with pytest.raises(ValueError, match="unknown grocery list change"):
        brain_inbox.add_grocery_event(week_start=WEEK, change="ordered?", text="t", at=datetime.now(timezone.utc),
                                      speaker="x", connect=connect)


def test_the_brain_reads_a_week_as_one_matter_with_its_own_source(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[], connect=connect)
    grocery_lists.approve(WEEK, datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc), connect=connect)
    events = [dict(e, route="new_matter", gate_status="routed", concern_ids=[]) for e in _events(connect)]
    assert brain_inbox.ready(events, datetime.now(timezone.utc)) == events      # never waits like chat
    assert len(brain_step.group_matters(events)) == 1
    assert brain_step._source(events[0])["kind"] == "grocery"
    assert brain_step._evidence(events[0])["kind"] == "grocery_list"


def test_the_conversation_view_shows_the_steps(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[], connect=connect)
    events = [dict(e, mark="E1") for e in _events(connect)]
    [group] = conversations.build(events)
    assert group["heading"] == "The weekly grocery list"
    assert "ready for the owner to review" in conversations.render([group])
