"""Belief shadow-run comparison (2026-06-12).

Records what both belief lanes produced per meal-agent run; the report
diffs them (approximately — phrasing differs between lanes) and
summarizes v2 engine activity. Pure plumbing, no LLM.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from belief_engine_v2.shadow import (
    diff_run,
    load_shadow_runs,
    record_shadow_run,
    v2_activity,
)


def _v2_row(stmt, *, score=0.5, kind="preference", status="active",
            last="2026-06-10T08:00:00", obs=2):
    return {
        "belief_id": f"b-{abs(hash(stmt)) % 10_000}",
        "statement_nl": stmt,
        "kind": kind,
        "status": status,
        "last_observed": last,
        "obs_count": obs,
        "score": score,
        "_scores": {"temporal": 1.0, "recency": 0.5, "frequency": 0.3, "relevance": 0.7},
    }


def test_record_load_roundtrip_and_window(tmp_path):
    path = tmp_path / "shadow.jsonl"
    record_shadow_run(
        agent="weekly_meal_planner",
        v2_rows=[_v2_row("The kids will not eat zucchini.")],
        v1_text="header\n- The kids will not eat zucchini.  [food, 3x]\n- Old thing.  [food]",
        path=path,
    )
    record_shadow_run(
        agent="daily_meal_proposer",
        v2_rows=[],
        v1_text="",
        now=datetime.now() - timedelta(days=30),
        path=path,
    )

    runs = load_shadow_runs(days=7, path=path)
    assert len(runs) == 1  # the 30-day-old one is outside the window
    run = runs[0]
    assert run["agent"] == "weekly_meal_planner"
    assert run["v2"][0]["statement"] == "The kids will not eat zucchini."
    assert run["v2"][0]["scores"]["recency"] == 0.5
    # v1 meta brackets are trimmed
    assert run["v1_items"] == ["The kids will not eat zucchini.", "Old thing."]


def test_diff_run_separates_lanes():
    run = {
        "agent": "weekly_meal_planner",
        "recorded_at": "2026-06-12T08:00:00",
        "v2": [
            {"statement": "The kids will not eat zucchini."},
            {"statement": "The household prefers spicy food on weekends."},
        ],
        "v1_items": [
            "The kids will not eat zucchini",          # same modulo punctuation
            "Friday night is meat night.",              # v1-only
        ],
    }
    d = diff_run(run)
    assert d["approximate"] is True
    assert d["only_v2"] == ["The household prefers spicy food on weekends."]
    assert d["only_v1"] == ["Friday night is meat night."]


def test_v2_activity_reads_store(tmp_path):
    db = tmp_path / "v2.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE observations (observation_id TEXT PRIMARY KEY, recorded_at TEXT,
            resolution_method TEXT);
        CREATE TABLE beliefs (belief_id TEXT PRIMARY KEY, statement_nl TEXT, kind TEXT,
            status TEXT, support REAL, contradiction REAL, obs_count INTEGER,
            first_observed TEXT, last_observed TEXT);
        CREATE TABLE belief_merges (belief_id TEXT PRIMARY KEY, merged_into TEXT,
            method TEXT, created_at TEXT);
    """)
    now = datetime.now()
    recent = (now - timedelta(days=1)).isoformat(timespec="seconds")
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    con.execute("INSERT INTO observations VALUES ('o1', ?, 'canonical')", (recent,))
    con.execute("INSERT INTO observations VALUES ('o2', ?, 'stub')", (recent,))
    con.execute(
        "INSERT INTO beliefs VALUES ('b1', 'New belief.', 'preference', 'active', 2, 0, 1, ?, ?)",
        (recent, recent))
    con.execute(
        "INSERT INTO beliefs VALUES ('b2', 'Old reinforced.', 'preference', 'active', 5, 0, 4, ?, ?)",
        (old, recent))
    con.execute(
        "INSERT INTO beliefs VALUES ('b3', 'Disputed.', 'preference', 'contested', 2, 2, 4, ?, ?)",
        (old, old))
    con.execute("INSERT INTO belief_merges VALUES ('b9', 'b2', 'llm_verified', ?)", (recent,))
    con.commit()
    con.close()

    act = v2_activity(days=7, db_path=db)
    assert act["observations_folded"] == 2
    assert act["resolver_method_mix"] == {"canonical": 1, "stub": 1}
    assert [b["belief_id"] for b in act["beliefs_minted"]] == ["b1"]
    assert [b["belief_id"] for b in act["beliefs_reinforced"]] == ["b2"]
    assert [b["belief_id"] for b in act["contested"]] == ["b3"]
    assert len(act["merges"]) == 1
    assert act["belief_totals_by_status"] == {"active": 2, "contested": 1}
