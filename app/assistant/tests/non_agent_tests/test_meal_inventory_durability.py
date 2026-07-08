"""Inventory durability + matching + fast-food pressure (2026-07-07 meal audit).

Pinned here:
- a CORRUPT inventory file RAISES instead of silently loading empty (the old
  behavior meant the daily 04:30 decay pass saved the empty state and
  permanently discarded every item + the history);
- a MISSING inventory still bootstraps empty;
- item matching is token-based: 'pepper' no longer consumes 'pepperoni',
  while 'salmon' still consumes 'wild salmon';
- history is capped on save so the file can't grow forever;
- fast-food pressure derives from plan slots overlaid with daily proposals
  (the old version counted distinct keywords in TODAY's diet log while
  labeled as a 7-day count).
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.assistant.subconscious import grocery_inventory as gi
from app.assistant.subconscious.meal_context_builder import _fast_food_pressure


# ---------------------------------------------------------------------------
# corrupt / missing inventory
# ---------------------------------------------------------------------------

def test_corrupt_inventory_raises(monkeypatch, tmp_path):
    path = tmp_path / "resources" / "subconscious" / "resource_grocery_inventory.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ nope", encoding="utf-8")
    monkeypatch.setattr(gi, "get_repo_root", lambda: tmp_path)
    with pytest.raises(json.JSONDecodeError):
        gi.load_inventory()
    # The corrupt bytes are still there for a human to recover.
    assert path.read_text(encoding="utf-8") == "{ nope"


def test_missing_inventory_bootstraps_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(gi, "get_repo_root", lambda: tmp_path)
    inv = gi.load_inventory()
    assert inv["items"] == [] and inv["history"] == []


def test_history_capped_on_save(monkeypatch, tmp_path):
    monkeypatch.setattr(gi, "get_repo_root", lambda: tmp_path)
    inv = gi._empty_inventory()
    inv["history"] = [{"name": f"item-{i}"} for i in range(gi._HISTORY_KEEP + 50)]
    gi.save_inventory(inv)
    saved = gi.load_inventory()
    assert len(saved["history"]) == gi._HISTORY_KEEP
    # Newest kept (the tail), oldest dropped.
    assert saved["history"][-1]["name"] == f"item-{gi._HISTORY_KEEP + 49}"


# ---------------------------------------------------------------------------
# token matching
# ---------------------------------------------------------------------------

def _items(*names):
    return [{"name": n, "consumed_at_utc": None, "acquired_at_utc": ""} for n in names]


def test_pepper_does_not_consume_pepperoni():
    """Whole tokens must be EQUAL — the substring absurdities are gone.
    (Token-subset borderlines like 'rice' → 'rice vinegar' remain accepted:
    the same shape as 'salmon' → 'wild salmon', indistinguishable by token
    math alone.)"""
    assert gi._find_item_matching(_items("pepperoni"), "pepper") is None
    assert gi._find_item_matching(_items("cream cheese"), "cream soda") is None


def test_salmon_consumes_wild_salmon_both_directions():
    assert gi._find_item_matching(_items("wild salmon"), "salmon")["name"] == "wild salmon"
    assert gi._find_item_matching(_items("potato"), "sweet potato")["name"] == "potato"


def test_short_names_require_exact_match():
    assert gi._find_item_matching(_items("oj"), "oj")["name"] == "oj"
    assert gi._find_item_matching(_items("orange juice"), "oj") is None


# ---------------------------------------------------------------------------
# fast-food pressure (pure derivation)
# ---------------------------------------------------------------------------

class _Pod:
    def __init__(self, metadata):
        self.metadata = metadata


def test_fast_food_pressure_counts_window_and_overlays():
    today = date(2026, 7, 8)
    plan = [_Pod({"slots": [
        {"date": "2026-07-06", "meal_window": "dinner", "slot_type": "fast_food", "dish": "Burgers"},
        {"date": "2026-07-07", "meal_window": "dinner", "slot_type": "home_cook", "dish": "Pasta"},
        {"date": "2026-06-20", "meal_window": "dinner", "slot_type": "fast_food", "dish": "Old pizza"},  # outside window
    ]})]
    intents = [
        # Daily proposal REPLACED the planned fast-food dinner with a home cook…
        _Pod({"date": "2026-07-06", "meal_window": "dinner", "source": "home_cook", "dish": "Stir fry"}),
        # …and added a takeout lunch the plan didn't have.
        _Pod({"date": "2026-07-07", "meal_window": "lunch", "source": "takeout", "dish": "Thai"}),
    ]
    pressure = _fast_food_pressure(plan, intents, today=today)
    assert pressure["count"] == 1
    assert pressure["hits"] == [("2026-07-07", "lunch", "takeout", "Thai")]


def test_fast_food_pressure_zero_when_all_home():
    today = date(2026, 7, 8)
    plan = [_Pod({"slots": [
        {"date": "2026-07-07", "meal_window": "dinner", "slot_type": "home_cook", "dish": "Soup"},
    ]})]
    assert _fast_food_pressure(plan, [], today=today)["count"] == 0
