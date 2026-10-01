"""Beliefs grouped into named topics, read together by the routine's selector and writer (2026-09-30).

The selector picked "timesheets weekly, 3–4 PM" for a Wednesday without seeing "weekly timesheets are
due on Mondays", which was on another page of its catalog. Pins: code proposes groups by meaning; the
clusterer's answer places every belief once, joining existing topics by label; an invalid answer gets
one correction, then fails loudly; the export carries each belief's topic; the selector's pages never
split a topic; the writer gets a selected belief's topic-mates. Scratch store; the model is a fake;
invented data only.
"""
from __future__ import annotations

import json

import pytest

from belief_engine import clusters
from belief_engine.intake.catalog import active_entries
from belief_engine.intake.store import SCHEMA as INTAKE_SCHEMA, sqlite_file

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
    with connect(True) as c:
        for s in INTAKE_SCHEMA:
            c.execute(s.format(p="belief_intake_"))
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


def test_what_was_placed_once_is_kept_and_only_the_rest_is_asked_again(connect):
    answers = iter([
        {"clusters": [{"label": "Reports", "belief_ids": ["B1", "B2", "B3"]},
                      {"label": "reports", "belief_ids": ["B3", "B9"]}], "reasoning": "r"},   # B3 twice, B4 missed
        {"clusters": [{"label": "Meals", "belief_ids": ["B3", "B4"]}], "reasoning": "r"}])
    seen = []
    assert clusters.place_beliefs(connect=connect, call=lambda p: seen.append(p) or next(answers))["placed"] == 4
    assert [[b["id"] for b in g] for g in seen[1]["beliefs_to_place"]] == [["B3", "B4"]]
    assert "placed more than once: ['B3']" in seen[1]["correction"] and "not placed: ['B4']" in seen[1]["correction"]
    assert "['B9'] are not beliefs to place" in seen[1]["correction"]
    assert {c["label"] for c in seen[1]["existing_clusters"]} == {"Reports"}   # the first answer is kept
    assert {b: t["cluster"] for b, t in clusters.memberships(connect).items()} == {
        "B1": "Reports", "B2": "Reports", "B3": "Meals", "B4": "Meals"}


def test_a_belief_still_unplaced_after_the_correction_fails_the_run(connect):
    with pytest.raises(ValueError, match="still unplaced"):
        clusters.place_beliefs(connect=connect, call=lambda p: {"clusters": [], "reasoning": "r"})


def test_the_export_carries_each_beliefs_topic(connect):
    clusters.place_beliefs(connect=connect, call=lambda p: {"clusters": [
        {"label": "Reports", "belief_ids": ["B1", "B2"]}, {"label": "Meals", "belief_ids": ["B3", "B4"]}],
        "reasoning": "r"})
    with connect(False) as c:
        entries = active_entries(conn=c)
    assert {e["belief_key"]: e["cluster"] for e in entries} == {"B1": "Reports", "B2": "Reports",
                                                                "B3": "Meals", "B4": "Meals"}


def test_the_selector_reads_whole_topics_and_names_the_conditions_for_the_writer(monkeypatch):
    from types import SimpleNamespace
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.pipelines.dayflow.steps import dayflow_routine_stage as stage
    entries = [{"belief_key": f"B{i}", "statement": "x" * 300, "cluster_id": k, "cluster": label}
               for i, (k, label) in enumerate([("K1", "Reports"), ("K2", "Meals"), ("K1", "Reports"),
                                               ("K2", "Meals"), (None, None)], 1)]
    pages = stage._topic_pages(entries, max_chars=900)
    assert [[e["belief_key"] for e in p] for p in pages] == [["B2", "B4"], ["B1", "B3"], ["B5"]]

    def answer(msg):                      # on the Reports page: B1 for today, B3 its condition
        ids = {r["belief_key"]: r["selection_id"] for r in msg.agent_input["belief_catalog"]}
        if "B1" in ids:
            return SimpleNamespace(data={"belief_ids": [ids["B1"]], "qualifier_ids": [ids["B3"], ids["B1"]],
                                         "reasoning": "reports today"})
        return SimpleNamespace(data={"belief_ids": [], "qualifier_ids": [], "reasoning": "nothing"})
    monkeypatch.setattr(DI, "agent_factory", SimpleNamespace(
        create_agent=lambda _: SimpleNamespace(action_handler=answer)), raising=False)
    monkeypatch.setattr(stage, "_topic_pages", lambda e, max_chars: pages)
    selected, _, qualifiers = stage._select_beliefs(entries, "today", None)
    assert [e["belief_key"] for e in selected] == ["B1"]
    assert [e["belief_key"] for e in qualifiers] == ["B3"]            # a selected belief is not its own condition
