"""Meal planner ↔ belief-engine retrieval lane (first beliefs_for_context consumer).

Covers:
- the lane renders context-ranked beliefs from the v1 store, marking
  recently-confirmed items as current intent and stale ones as not
- the dispatcher honors subsystem flag meal_beliefs_v2 (off → legacy v1
  prefix-match lane)
- a lane failure with the flag ON is loud (explicit marker, no silent swap)

The lane reads the v1 store (emi.db: user_beliefs + belief_tags + belief_short_id)
via belief_engine.retrieval.beliefs_for_context — belief-engine v2 was retired as
the primary producer (2026-06-16). The `_v2` names persist for flag continuity.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import app.assistant.subconscious.meal_context_builder as mcb


def _fake_embedder(texts):
    # Batch embedder (list -> list of vectors). Deterministic; relevance ordering
    # is not under test here, only presence + recency framing.
    return [[((sum(map(ord, t)) >> i) & 0xFF) / 255.0 + 0.01 for i in range(8)] for t in texts]


NOW = datetime(2026, 6, 10, 9, 0, 0)


@pytest.fixture()
def v1_store_path(tmp_path):
    db = str(tmp_path / "belief_v1_test.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE user_beliefs (
            id TEXT PRIMARY KEY, belief_key TEXT, statement TEXT, domain TEXT,
            confidence TEXT, kind TEXT, observation_count INTEGER,
            last_confirmed TEXT, status TEXT
        );
        CREATE TABLE belief_short_id (belief_id TEXT PRIMARY KEY, short_id INTEGER);
        CREATE TABLE belief_tags (belief_id TEXT, tag TEXT);
        """
    )
    fresh = (NOW - timedelta(days=2)).isoformat()
    stale = (NOW - timedelta(days=120)).isoformat()
    con.executemany(
        "INSERT INTO user_beliefs VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("b-kids-bananas", "food.kids.bananas", "The kids strongly dislike bananas.",
             "food", "high", "preference", 4, fresh, "active"),
            ("b-user-salmon", "food.user.salmon", "The user prefers simple oven-baked salmon.",
             "food", "high", "preference", 2, stale, "active"),
        ],
    )
    con.executemany("INSERT INTO belief_short_id VALUES (?,?)",
                    [("b-kids-bananas", 11), ("b-user-salmon", 12)])
    con.commit()
    con.close()
    return db


def test_lane_renders_recent_and_stale(v1_store_path):
    # belief_tags is empty → the high-recall guard returns the whole active set,
    # so the formatter (not the tag scope) is what's under test here.
    con = sqlite3.connect(v1_store_path)
    con.row_factory = sqlite3.Row
    try:
        block = mcb._build_food_beliefs_v2(
            agent="weekly_meal_planner", conn=con,
            embedder=_fake_embedder, now=NOW,
        )
    finally:
        con.close()

    assert "The kids strongly dislike bananas." in block
    assert "The user prefers simple oven-baked salmon." in block
    # Fresh last_confirmed flagged as current intent; stale one not.
    banana_line = next(l for l in block.splitlines() if "bananas" in l)
    salmon_line = next(l for l in block.splitlines() if "salmon" in l)
    assert "recent" in banana_line
    assert "recent" not in salmon_line
    assert "preference" in banana_line          # kind is visible to the planner
    # Observed DATE is visible — this is what lets the planner LLM time-scope
    # transient facts ("sick Monday" governs Tue, not Thu).
    assert "observed 2026-06-08" in banana_line
    assert "observed 2026-02-" in salmon_line
    # The header carries today's date + the time-scoping doctrine.
    header = block.splitlines()[0]
    assert "2026-06-10" in header
    assert "stomach bug" in header


def test_dispatcher_flag_off_uses_legacy_lane(monkeypatch):
    import app.assistant.utils.subsystem_flags as flags
    monkeypatch.setattr(flags, "is_subsystem_enabled",
                        lambda name: False if name == "meal_beliefs_v2" else True)
    called = {}
    monkeypatch.setattr(mcb, "_build_food_beliefs_v1",
                        lambda: called.setdefault("v1", True) and "V1 BLOCK")
    assert mcb._build_food_beliefs() == "V1 BLOCK"
    assert called.get("v1") is True


def test_lane_failure_is_loud_not_silent(monkeypatch):
    import app.assistant.utils.subsystem_flags as flags
    monkeypatch.setattr(flags, "is_subsystem_enabled", lambda name: True)
    monkeypatch.setattr(
        mcb, "_build_food_beliefs_v2",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("store missing")),
    )
    block = mcb._build_food_beliefs()
    assert "BELIEF LANE ERROR" in block          # explicit marker in the prompt
    assert "meal_beliefs_v2" in block            # tells the operator the switch
