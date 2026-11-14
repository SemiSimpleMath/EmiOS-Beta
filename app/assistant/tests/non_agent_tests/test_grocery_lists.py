"""The week's grocery list store (subconscious/grocery_lists.py, 2026-11-14), on a scratch database."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import grocery_inventory, grocery_lists
from belief_engine.intake.store import sqlite_file

WEEK = "2026-11-16"
ITEMS = [
    {"name": "salmon fillet", "quantity": "1 lb", "category": "Meat & Seafood", "reason": "Monday's salmon"},
    {"name": "milk", "quantity": "1 gallon", "category": "Dairy & Eggs", "reason": "staple; none in the inventory"},
]
SKIPPED = [{"name": "cookies", "reason": "bought the last three weeks"}]


@pytest.fixture
def connect(tmp_path):
    return sqlite_file(tmp_path / "grocery.db")


def test_planned_list_is_stored_as_a_draft(connect):
    result = grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=SKIPPED, planner_note="light week",
                                        plan_pod_id="pod1", connect=connect)
    assert result == {"week_start": WEEK, "status": "draft", "item_count": 2, "skipped_count": 1}
    gl = grocery_lists.get(WEEK, connect=connect)
    assert gl["status"] == "draft"
    assert [i["name"] for i in gl["items"]] == ["salmon fillet", "milk"]
    assert gl["skipped_staples"] == SKIPPED
    assert gl["plan_pod_id"] == "pod1" and gl["planner_note"] == "light week" and gl["edits"] == []


def test_a_rerun_replaces_a_draft(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=SKIPPED, connect=connect)
    grocery_lists.save_planned(WEEK, items=ITEMS[:1], skipped_staples=[], connect=connect)
    gl = grocery_lists.get(WEEK, connect=connect)
    assert [i["name"] for i in gl["items"]] == ["salmon fillet"] and gl["skipped_staples"] == []


def test_a_rerun_never_replaces_an_approved_list(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=SKIPPED, connect=connect)
    with connect(True) as c:
        c.execute("UPDATE grocery_lists SET status = 'approved' WHERE week_start = ?", (WEEK,))
    with pytest.raises(ValueError, match="approved"):
        grocery_lists.save_planned(WEEK, items=[], skipped_staples=[], connect=connect)
    assert len(grocery_lists.get(WEEK, connect=connect)["items"]) == 2


def test_an_item_without_a_name_is_refused(connect):
    with pytest.raises(ValueError, match="no name"):
        grocery_lists.save_planned(WEEK, items=[{"name": " ", "quantity": "1"}], skipped_staples=[], connect=connect)


def test_the_planner_sees_past_weeks_and_the_owners_changes(connect):
    grocery_lists.save_planned("2026-11-09", items=ITEMS, skipped_staples=[], connect=connect)
    edits = [{"at": "2026-11-09T18:00:00+00:00", "change": "removed", "text": "cookies"},
             {"at": "2026-11-09T18:01:00+00:00", "change": "added", "text": "bananas (1 bunch)"}]
    with connect(True) as c:
        c.execute("UPDATE grocery_lists SET status = 'delivered', edits = ? WHERE week_start = '2026-11-09'",
                  (json.dumps(edits),))
    grocery_lists.save_planned("2026-10-05", items=ITEMS, skipped_staples=[], connect=connect)  # older than 4 weeks
    text = grocery_lists.render_recent_for_planner(before_week=WEEK, connect=connect)
    assert text.splitlines() == [
        "Week of 2026-11-09 (delivered):",
        "  items: salmon fillet (1 lb), milk (1 gallon)",
        "  owner removed: cookies",
        "  owner added: bananas (1 bunch)",
    ]


def test_no_earlier_lists(connect):
    assert grocery_lists.render_recent_for_planner(before_week=WEEK, connect=connect) == "(no earlier grocery lists yet)"


def test_recent_purchases_come_from_the_inventory(monkeypatch):
    now = datetime.now(timezone.utc)
    inv = {"items": [{"name": "milk", "acquired_at_utc": (now - timedelta(days=2)).isoformat()}],
           "history": [{"name": "cookies", "acquired_at_utc": (now - timedelta(days=9)).isoformat()},
                       {"name": "old bread", "acquired_at_utc": (now - timedelta(days=40)).isoformat()}]}
    monkeypatch.setattr(grocery_inventory, "load_inventory", lambda: inv)
    lines = grocery_inventory.render_recent_purchases(days=28).splitlines()
    assert [l.split(": ", 1)[1] for l in lines] == ["milk", "cookies"]


def test_no_recent_purchases(monkeypatch):
    monkeypatch.setattr(grocery_inventory, "load_inventory", lambda: {"items": [], "history": []})
    assert grocery_inventory.render_recent_purchases(days=28) == "(nothing recorded as bought in the last 28 days)"


# ── the owner's edits on /meals ───────────────────────────────────────────


def _draft(connect):
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=SKIPPED, connect=connect)
    return {i["name"]: i["id"] for i in grocery_lists.get(WEEK, connect=connect)["items"]}


def test_every_item_has_its_own_id(connect):
    ids = _draft(connect)
    assert len(set(ids.values())) == 2


def test_owner_edits_change_the_list_and_are_recorded(connect):
    ids = _draft(connect)
    grocery_lists.remove_item(WEEK, ids["milk"], connect=connect)
    grocery_lists.set_quantity(WEEK, ids["salmon fillet"], "2 lb", connect=connect)
    grocery_lists.set_quantity(WEEK, ids["salmon fillet"], "2 lb", connect=connect)  # unchanged: no record
    grocery_lists.add_item(WEEK, name="bananas", quantity="1 bunch", connect=connect)
    gl = grocery_lists.get(WEEK, connect=connect)
    assert [(i["name"], i["quantity"]) for i in gl["items"]] == [("salmon fillet", "2 lb"), ("bananas", "1 bunch")]
    assert gl["items"][1]["reason"] == "added by the owner"
    assert [(e["change"], e["text"]) for e in gl["edits"]] == [
        ("removed", "milk (1 gallon)"),
        ("changed", "salmon fillet: 1 lb -> 2 lb"),
        ("added", "bananas (1 bunch)"),
    ]
    assert all(e["at"] for e in gl["edits"])


def test_approve_sets_the_order_time_and_locks_the_list(connect):
    ids = _draft(connect)
    at = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)
    grocery_lists.approve(WEEK, at, connect=connect)
    gl = grocery_lists.get(WEEK, connect=connect)
    assert gl["status"] == "approved" and gl["order_at"] == at.isoformat() and gl["approved_at"]
    with pytest.raises(ValueError, match="approved"):
        grocery_lists.remove_item(WEEK, ids["milk"], connect=connect)


def test_approve_needs_an_aware_time(connect):
    _draft(connect)
    with pytest.raises(ValueError, match="aware"):
        grocery_lists.approve(WEEK, datetime(2026, 11, 16, 10, 30), connect=connect)


def test_skip_and_reopen(connect):
    _draft(connect)
    grocery_lists.approve(WEEK, datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc), connect=connect)
    grocery_lists.skip(WEEK, connect=connect)
    gl = grocery_lists.get(WEEK, connect=connect)
    assert gl["status"] == "skipped" and gl["order_at"] is None
    grocery_lists.reopen(WEEK, connect=connect)
    assert grocery_lists.get(WEEK, connect=connect)["status"] == "draft"
    with pytest.raises(ValueError):
        grocery_lists.reopen(WEEK, connect=connect)  # a draft is already open


def test_unknown_item_and_missing_week_are_refused(connect):
    _draft(connect)
    with pytest.raises(ValueError, match="no item"):
        grocery_lists.remove_item(WEEK, "nope", connect=connect)
    with pytest.raises(ValueError, match="no grocery list"):
        grocery_lists.skip("2030-01-07", connect=connect)


def test_default_order_time_comes_from_the_owners_setting(monkeypatch):
    from app.assistant.utils import resource_files
    from app.assistant.utils.time_utils import utc_to_local
    monkeypatch.setattr(resource_files, "read", lambda rel: {"default_order": {"weekday": "Monday", "time": "10:30"}})
    local = utc_to_local(grocery_lists.default_order_at(WEEK))
    assert (local.date().isoformat(), local.hour, local.minute) == (WEEK, 10, 30)
