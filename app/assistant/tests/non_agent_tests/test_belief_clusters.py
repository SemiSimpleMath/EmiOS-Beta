"""Beliefs grouped into named topics, read together by the routine's selector and writer (2026-09-30).

The selector picked "timesheets weekly, 3–4 PM" for a Wednesday without seeing "weekly timesheets are
due on Mondays", which was on another page of its catalog. Pins: code proposes groups by meaning; the
clusterer's answer places every belief once, joining existing topics by label; its validate.py refuses
a belief missed or placed twice and a blank label (corrected once by agent_runtime/correction.py since
2026-11-15); the export carries each belief's topic. Scratch store; the model is a fake; invented data
only.
"""
from __future__ import annotations

import json

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.path_utils import get_app_root
from belief_engine import clusters
from belief_engine.intake.catalog import active_entries
from belief_engine.intake.store import IntakeStore, sqlite_file

WORK, MEAL = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
BELIEFS = [
    ("B1", "Weekly reports are due on Mondays.", [1.0, 0.05, 0.0]),
    ("B2", "Monthly reports are due by 5 PM; the owner likes to finish them 3–4 PM.", [0.98, 0.1, 0.0]),
    ("B3", "The owner eats lunch around noon.", [0.0, 1.0, 0.05]),
    ("B4", "The owner prefers light dinners.", [0.05, 0.97, 0.0]),
]


@pytest.fixture
def connect(tmp_path):
    connect = sqlite_file(tmp_path / "beliefs.db")
    IntakeStore(connect, "belief_intake_")       # the intake's tables, with every column it adds
    with connect(True) as c:
        c.execute("CREATE TABLE belief_tags (belief_id TEXT, tag TEXT, assigned_at TEXT, method TEXT)")
        c.executemany("INSERT INTO belief_intake_beliefs (id, statement, kind, created_day, embedding) "
                      "VALUES (?, ?, 'routine_pattern', '2026-09-30', ?)",
                      [(bid, st, json.dumps(v)) for bid, st, v in BELIEFS])
    return connect


def test_code_proposes_groups_by_meaning():
    groups = clusters.propose_groups([{"id": b, "statement": s, "embedding": v} for b, s, v in BELIEFS])
    assert [[b["id"] for b in g] for g in groups] == [["B1", "B2"], ["B3", "B4"]]


def test_a_first_placement_names_topics_and_a_later_one_joins_them(connect):
    seen = []

    def call(payload):
        seen.append(payload)
        return {"clusters": [{"label": "Reports", "belief_ids": ["B1", "B2"]},
                             {"label": "Meals", "belief_ids": ["B3", "B4"]}], "reasoning": "r"}

    assert clusters.place_beliefs(connect=connect, call=call) == {"placed": 4, "clusters_created": 2}
    assert seen[0]["existing_clusters"] == []
    assert [[b["id"] for b in g] for g in seen[0]["beliefs_to_place"]] == [["B1", "B2"], ["B3", "B4"]]
    assert clusters.place_beliefs(connect=connect, call=call) == {"placed": 0, "clusters_created": 0}

    with connect(True) as c:
        c.execute("INSERT INTO belief_intake_beliefs (id, statement, kind, created_day, embedding) VALUES "
                  "('B5', 'Reports may be sent a day late when the owner travels.', 'routine_pattern', '2026-09-30', ?)",
                  (json.dumps(WORK),))
    later = clusters.place_beliefs(connect=connect, call=lambda p: seen.append(p) or {
        "clusters": [{"label": "reports", "belief_ids": ["B5"]}], "reasoning": "r"})
    assert later == {"placed": 1, "clusters_created": 0}                     # joined by label, any case
    assert {c["label"]: c["beliefs"] for c in seen[-1]["existing_clusters"]}["Reports"] == [
        "Weekly reports are due on Mondays.", "Monthly reports are due by 5 PM; the owner likes to finish them 3–4 PM."]
    topics = clusters.memberships(connect)
    assert topics["B5"] == topics["B1"] == {"cluster_id": "K1", "cluster": "Reports"}


RULES = AgentRegistry()._load_validator(
    get_app_root() / "assistant" / "agents" / "belief_engine" / "belief_clusterer" / "validate.py")
PAGE = {"existing_clusters": [], "beliefs_to_place": [[{"id": "B1", "statement": "s"}, {"id": "B2", "statement": "s"}],
                                                      [{"id": "B3", "statement": "s"}, {"id": "B4", "statement": "s"}]]}


def test_the_clusterer_refuses_a_belief_missed_or_placed_twice_and_a_blank_label():
    ok = {"clusters": [{"label": "Reports", "belief_ids": ["B1", "B2"]},
                       {"label": "reports", "belief_ids": ["B3", "B9"]},          # same topic; B9 is ignored
                       {"label": "Meals", "belief_ids": ["B4"]}], "reasoning": "r"}
    assert RULES.validate(ok, PAGE) == []
    bad = RULES.validate({"clusters": [{"label": "Reports", "belief_ids": ["B1", "B2", "B3"]},
                                       {"label": "  ", "belief_ids": ["B3"]}], "reasoning": "r"}, PAGE)
    assert [(p.at, p.value, p.structural) for p in bad] == [
        ("[1].label", "  ", False), ("", "B3", True), ("", "B4", True)]
    assert "more than once" in bad[1].why and "not placed" in bad[2].why


def test_labels_join_in_any_case_and_an_id_not_given_is_not_written(connect):
    assert clusters.place_beliefs(connect=connect, call=lambda p: {"clusters": [
        {"label": "Reports", "belief_ids": ["B1", "B2", "B9"]}, {"label": "reports", "belief_ids": ["B3"]},
        {"label": "Meals", "belief_ids": ["B4"]}], "reasoning": "r"}) == {"placed": 4, "clusters_created": 2}
    assert {b: t["cluster"] for b, t in clusters.memberships(connect).items()} == {
        "B1": "Reports", "B2": "Reports", "B3": "Reports", "B4": "Meals"}


def test_the_export_carries_each_beliefs_topic(connect):
    clusters.place_beliefs(connect=connect, call=lambda p: {"clusters": [
        {"label": "Reports", "belief_ids": ["B1", "B2"]}, {"label": "Meals", "belief_ids": ["B3", "B4"]}],
        "reasoning": "r"})
    with connect(False) as c:
        entries = active_entries(conn=c)
    assert {e["belief_key"]: e["cluster"] for e in entries} == {"B1": "Reports", "B2": "Reports",
                                                                "B3": "Meals", "B4": "Meals"}
