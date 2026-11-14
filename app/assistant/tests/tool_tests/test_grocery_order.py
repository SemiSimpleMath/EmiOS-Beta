"""The grocery_order tool and the order's last two steps (2026-11-14), on a scratch database; the inventory write is
captured, never made."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.assistant.lib.tools.grocery_order.grocery_order import GroceryOrderTool
from app.assistant.subconscious import grocery_inventory, grocery_lists
from app.assistant.tests.tool_tests._helpers import make_tool_message
from belief_engine.intake.store import sqlite_file

WEEK = "2026-11-16"
ITEMS = [{"name": "milk", "quantity": "1 gallon", "category": "Dairy & Eggs", "reason": "staple"},
         {"name": "salmon fillet", "quantity": "1 lb", "category": "Meat & Seafood", "reason": "Monday's salmon"}]


@pytest.fixture
def acquired(tmp_path, monkeypatch):
    monkeypatch.setattr(grocery_lists, "_connect", sqlite_file(tmp_path / "grocery.db"))
    got = []
    monkeypatch.setattr(grocery_inventory, "manual_acquire",
                        lambda items, source_note: got.append((list(items), source_note)))
    grocery_lists.save_planned(WEEK, items=ITEMS, skipped_staples=[])
    return got


def _approve():
    grocery_lists.approve(WEEK, datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc))


def _run(**arguments):
    return GroceryOrderTool().execute(make_tool_message("grocery_order", arguments))


def test_get_gives_the_approved_list_to_order(acquired):
    _approve()
    result = _run(action="get")
    assert result.result_type == "success" and result.data["status"] == "approved"
    assert "Order exactly these items:" in result.content
    assert "- milk (1 gallon)" in result.content and "- salmon fillet (1 lb)" in result.content


def test_a_draft_is_not_to_be_ordered(acquired):
    result = _run(action="get", week_start=WEEK)
    assert "Not approved yet; nothing is to be ordered" in result.content


def test_no_open_order(acquired):
    result = _run(action="get")
    assert result.result_type == "error" and "nothing to order" in result.content.lower()


def test_ordered_then_delivered_fills_the_inventory(acquired):
    _approve()
    result = _run(action="ordered", note="DoorDash order 1234, total $142.18, arriving 12-1 PM")
    assert result.data["status"] == "ordered" and "order 1234" in result.content
    result = _run(action="delivered", items=["milk", "Atlantic salmon fillet (substitute)"])
    assert result.data["status"] == "delivered"
    assert acquired == [(["milk", "Atlantic salmon fillet (substitute)"], f"grocery delivery, week of {WEEK}")]
    row = grocery_lists.get(WEEK)
    assert row["delivered_items"] == ["milk", "Atlantic salmon fillet (substitute)"] and row["ordered_at"]


def test_steps_out_of_order_are_refused(acquired):
    result = _run(action="ordered", week_start=WEEK, note="x")       # a draft cannot be ordered
    assert result.result_type == "error" and "draft" in result.content
    _approve()
    result = _run(action="delivered", items=["milk"])                 # not ordered yet
    assert result.result_type == "error" and acquired == []


def test_ordered_needs_what_the_store_confirmed(acquired):
    _approve()
    assert _run(action="ordered", note=" ").result_type == "error"


def test_delivered_needs_the_items(acquired):
    _approve()
    _run(action="ordered", note="order 1")
    assert _run(action="delivered", items=[]).result_type == "error" and acquired == []


def test_an_ordered_week_cannot_be_skipped(acquired):
    _approve()
    _run(action="ordered", note="order 1")
    with pytest.raises(ValueError, match="ordered"):
        grocery_lists.skip(WEEK)


def test_bad_action(acquired):
    assert _run(action="buy").result_type == "error"


def test_a_grocery_task_infers_the_groceries_domain():
    from app.assistant.lib.task_utils.tool_domain_filter import extract_domains
    assert "groceries" in extract_domains("Record the delivered Ralphs groceries from the receipt")
