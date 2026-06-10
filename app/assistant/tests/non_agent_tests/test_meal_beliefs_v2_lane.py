"""Meal planner ↔ belief-engine v2 cutover pilot (first beliefs_for_context
consumer).

Covers:
- the v2 lane renders context-ranked beliefs from a v2 store, marking
  recently-observed items as current intent
- every surfacing is logged to surfacing_log with the consuming agent
  (the contextual-bandit seed: log first, learn later)
- the dispatcher honors subsystem flag meal_beliefs_v2 (off → legacy v1 lane)
- a v2 store failure with the flag ON is loud (explicit marker, no silent
  swap to v1)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import app.assistant.subconscious.meal_context_builder as mcb
from belief_engine_v2.ingest import open_live_store
from belief_engine_v2.store import Claim


def _fake_embedder(text: str):
    # Deterministic tiny vector; relevance ordering is not under test here.
    h = abs(hash(text))
    return [((h >> (8 * i)) & 0xFF) / 255.0 + 0.01 for i in range(8)]


NOW = datetime(2026, 6, 10, 9, 0, 0)


@pytest.fixture()
def v2_store_path(tmp_path):
    db = str(tmp_path / "belief_v2_test.db")
    store = open_live_store(db, embedder=_fake_embedder, with_verifier=False)
    fresh = (NOW - timedelta(days=2)).isoformat()
    stale = (NOW - timedelta(days=120)).isoformat()
    store.append(
        Claim(subject="the kids", predicate="dislike", object="bananas",
              statement_nl="The kids strongly dislike bananas.",
              extra={"kind": "preference"}),
        source="daily_insight", polarity="affirm", occurred_at=fresh,
        extractor_version="test",
    )
    store.append(
        Claim(subject="the user", predicate="prefers", object="oven-baked salmon",
              statement_nl="The user prefers simple oven-baked salmon.",
              extra={"kind": "preference"}),
        source="daily_insight", polarity="affirm", occurred_at=stale,
        extractor_version="test",
    )
    store.rebuild_projection()
    store.close()
    return db


def test_v2_lane_renders_and_logs_surfacing(v2_store_path):
    block = mcb._build_food_beliefs_v2(
        agent="weekly_meal_planner", db_path=v2_store_path,
        embedder=_fake_embedder, now=NOW,
    )
    assert "The kids strongly dislike bananas." in block
    assert "The user prefers simple oven-baked salmon." in block
    # Fresh observation flagged as current intent; stale one not.
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

    con = sqlite3.connect(v2_store_path)
    rows = con.execute(
        "SELECT agent, belief_id, rank, outcome FROM surfacing_log ORDER BY rank"
    ).fetchall()
    con.close()
    assert len(rows) == 2                        # every surfaced belief logged
    assert all(r[0] == "weekly_meal_planner" for r in rows)
    assert all(r[3] is None for r in rows)       # outcome filled later, not now


def test_dispatcher_flag_off_uses_legacy_lane(monkeypatch):
    import app.assistant.utils.subsystem_flags as flags
    monkeypatch.setattr(flags, "is_subsystem_enabled",
                        lambda name: False if name == "meal_beliefs_v2" else True)
    called = {}
    monkeypatch.setattr(mcb, "_build_food_beliefs_v1",
                        lambda: called.setdefault("v1", True) and "V1 BLOCK")
    assert mcb._build_food_beliefs() == "V1 BLOCK"
    assert called.get("v1") is True


def test_v2_failure_is_loud_not_silent(monkeypatch, tmp_path):
    import app.assistant.utils.subsystem_flags as flags
    monkeypatch.setattr(flags, "is_subsystem_enabled", lambda name: True)
    # Point the lane at a nonexistent store via the production path resolution.
    monkeypatch.setattr(
        mcb, "_build_food_beliefs_v2",
        lambda **kw: (_ for _ in ()).throw(FileNotFoundError("store missing")),
    )
    block = mcb._build_food_beliefs()
    assert "BELIEF LANE ERROR" in block          # explicit marker in the prompt
    assert "meal_beliefs_v2" in block            # tells the operator the switch
