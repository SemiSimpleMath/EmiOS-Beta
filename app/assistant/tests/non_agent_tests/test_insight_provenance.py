"""Insights keep their provenance from the timeline to the belief (owner, 2026-10-03: "Everything needs to have
provenance").

Pins: the day's entries are numbered once, the assistant's replies included; the insights writer's citations are
stored on each insight as its sources (number, source ref, the entry itself), and an item citing nothing or an
unknown number is refused by the writer's validate.py (2026-11-15); the belief extractor reads each insight with exactly its own sources, and a
belief may cite only entries from its insight's sources. Scratch day folder; invented data only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.pipelines.daily_insights.steps.archive_daily_insights import cite_sources
from belief_engine.intake import day_items

_VALIDATE = (Path(__file__).resolve().parents[2] / "agents" / "daily_timeline_insights" / "validate.py")

TIMELINE = {"date": "2026-01-02", "timeline": [
    {"type": "context", "start_time_local": "2026-01-02 07:00", "end_time_local": "2026-01-02 08:00",
     "start_time_utc": "2026-01-02T15:00:00+00:00", "label": "AFK"},
    {"type": "chat", "start_time_local": "2026-01-02 09:05", "start_time_utc": "2026-01-02T17:05:00+00:00",
     "message_id": "m-user", "message": "On Fridays if there is a meeting I walk the dogs after it."},
    {"type": "ticket", "start_time_local": "2026-01-02 10:00", "start_time_utc": "2026-01-02T18:00:00+00:00",
     "ticket_id": "t-1", "title": "Trash", "message": "Take the bins out?", "user_action": "skip", "state": "dismissed"},
]}
REPLY = {"type": "assistant", "start_time_local": "2026-01-02 09:00", "start_time_utc": "2026-01-02T17:00:00+00:00",
         "message_id": "m-bot", "message": "Friday Night Meats runs until 10 PM; keep the 9 PM walk?"}


@pytest.fixture
def day(monkeypatch, tmp_path):
    (tmp_path / "timeline_merged.json").write_text(json.dumps(TIMELINE), encoding="utf-8")
    monkeypatch.setattr(day_items, "day_dir", lambda d: tmp_path)
    monkeypatch.setattr(day_items, "assistant_turns", lambda d: [REPLY])
    return tmp_path


def test_every_entry_is_numbered_once_with_the_assistants_replies_in_time_order(day):
    doc, entries = day_items.day_entries("2026-01-02")
    assert [(e["n"], e["type"]) for e in doc["timeline"]] == [(1, "context"), (2, "assistant"), (3, "chat"), (4, "ticket")]
    assert entries[3]["message_id"] == "m-user" and doc["date"] == "2026-01-02"
    assert [day_items.entry_ref(entries[n]) for n in (1, 2, 3, 4)] == [None, "message:m-bot", "message:m-user", "ticket:t-1"]


def test_an_insights_citations_are_stored_as_its_sources(day):
    _, entries = day_items.day_entries("2026-01-02")
    items = cite_sources([{"fact_summary": "walks the dogs after Friday Night Meats", "sources": [2, 3, 3]}], entries)
    assert [(s["n"], s["ref"]) for s in items[0]["sources"]] == [(2, "message:m-bot"), (3, "message:m-user")]
    assert items[0]["sources"][1]["entry"]["message"].startswith("On Fridays")


def test_the_writer_is_refused_an_insight_citing_nothing_or_an_unknown_number():
    validate = AgentRegistry()._load_validator(_VALIDATE).validate
    answer = {"actionable_information": [{"fact_summary": "walks the dogs", "sources": [2, 3, 3]},
                                         {"fact_summary": "skips trash prompts", "sources": []},
                                         {"fact_summary": "invented", "sources": [9, 4]}]}
    problems = validate(answer, {"entry_numbers": [1, 2, 3, 4]})
    assert [(p.at, p.value) for p in problems] == [("[1].sources", None), ("[2].sources", [9])]
    assert all(p.field == "actionable_information" and not p.item for p in problems)
    assert validate({"actionable_information": answer["actionable_information"][:1]}, {"entry_numbers": [2, 3]}) == []


def test_the_extractor_reads_each_insight_with_its_own_sources(day):
    _, entries = day_items.day_entries("2026-01-02")
    items = cite_sources([{"fact_summary": "walks the dogs after Friday Night Meats", "temporal_scope": "chronic",
                           "sources": [2, 3]},
                          {"fact_summary": "skips trash prompts", "sources": [4]}], entries)
    (day / "resource_daily_insights.json").write_text(json.dumps({"actionable_information": items}), encoding="utf-8")
    insights, provenance = day_items.insights("2026-01-02")
    first, second = insights
    assert first["insight"] == "walks the dogs after Friday Night Meats"
    assert [(s["n"], s["type"]) for s in first["sources"]] == [(2, "assistant reply"), (3, "chat message")]
    assert second["sources"][0]["response"] == "declined"
    assert {n: p["kind"] for n, p in provenance.items()} == {2: "assistant", 3: "said", 4: "did"}
    assert provenance[3]["source_ref"] == "message:m-user"


def test_an_insight_written_before_provenance_is_refused(day):
    (day / "resource_daily_insights.json").write_text(json.dumps({"actionable_information": [
        {"fact_summary": "old insight", "evidence": ["User: 'x'"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="no sources"):
        day_items.insights("2026-01-02")


def test_a_belief_may_cite_only_its_own_insights_sources():
    """The extractor's validate.py (2026-11-15; until then intake.agents re-asked by itself)."""
    rules = AgentRegistry()._load_validator(
        Path(__file__).resolve().parents[2] / "agents" / "belief_engine" / "belief_extractor" / "validate.py")
    given = {"date": "2026-01-02", "insights": [{"n": 1, "sources": [{"n": 2}, {"n": 3}]},
                                                {"n": 2, "sources": [{"n": 4}]}]}
    assert rules.validate({"beliefs": [{"insight_ref": 1, "sources": [3]}]}, given) == []
    bad = rules.validate({"beliefs": [{"insight_ref": 1, "sources": [4]}, {"insight_ref": 7, "sources": [2]},
                                      {"insight_ref": 2, "sources": [4, 4]}]}, given)
    assert [(p.at, p.value) for p in bad] == [("[0].sources", [4]), ("[1].insight_ref", 7), ("[2].sources", [4, 4])]
    assert "not among insight 1's sources [2, 3]" in bad[0].why and "[1, 2]" in bad[1].why
    assert "same entry twice" in bad[2].why


def test_dedup_names_a_listed_target_and_the_new_beliefs_own_sources():
    """The dedup's validate.py (2026-11-15; until then intake.agents.dedup re-asked by itself). The form refuses a
    verdict outside same/refines/contradicts/new."""
    rules = AgentRegistry()._load_validator(
        Path(__file__).resolve().parents[2] / "agents" / "belief_engine" / "belief_dedup" / "validate.py")
    given = {"new_belief": {"statement": "s", "kind": "routine_pattern", "sources": [{"n": 1}, {"n": 2}]},
             "existing_beliefs": [{"id": "B1"}, {"id": "B2"}]}
    assert rules.validate({"verdict": "same", "target": " B1 ", "sources": [2], "reasoning": "r"}, given) == []
    assert rules.validate({"verdict": "new", "target": None, "sources": [], "reasoning": "r"}, given) == []
    assert rules.validate({"verdict": "refines", "target": "B2", "sources": [], "reasoning": "r"}, given) == []
    bad = rules.validate({"verdict": "contradicts", "target": "B7", "sources": [], "reasoning": "r"}, given)
    assert [(p.field, p.value) for p in bad] == [("target", "B7"), ("sources", None)]
    assert "names no bearing sources" in bad[1].why
    out = rules.validate({"verdict": "same", "target": "B1", "sources": [0, 3], "reasoning": "r"}, given)
    assert [(p.field, p.why) for p in out] == [("sources", "sources [0, 3] are outside 1..2")]
