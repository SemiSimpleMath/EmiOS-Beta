"""Insights keep their provenance from the timeline to the belief (owner, 2026-10-03: "Everything needs to have
provenance").

Pins: the day's entries are numbered once, the assistant's replies included; the insights writer's citations are
stored on each insight as its sources (number, source ref, the entry itself), and an item citing nothing or an
unknown number is a problem to correct; the belief extractor reads each insight with exactly its own sources, and a
belief may cite only entries from its insight's sources. Scratch day folder; invented data only.
"""
from __future__ import annotations

import json

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.pipelines.daily_insights.steps.archive_daily_insights import cite_sources
from belief_engine.intake import agents, day_items

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
    items, problems = cite_sources([{"fact_summary": "walks the dogs after Friday Night Meats", "sources": [2, 3, 3]},
                                    {"fact_summary": "skips trash prompts", "sources": []},
                                    {"fact_summary": "invented", "sources": [9]}], entries)
    assert [(s["n"], s["ref"]) for s in items[0]["sources"]] == [(2, "message:m-bot"), (3, "message:m-user")]
    assert items[0]["sources"][1]["entry"]["message"].startswith("On Fridays")
    assert problems == ["insight 'skips trash prompts' cites no entries in sources",
                        "insight 'invented' cites [9], which are not entry numbers"]


def test_the_extractor_reads_each_insight_with_its_own_sources(day):
    _, entries = day_items.day_entries("2026-01-02")
    items, _ = cite_sources([{"fact_summary": "walks the dogs after Friday Night Meats", "temporal_scope": "chronic",
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
    sources_of = {1: {2, 3}, 2: {4}}
    ok = {"beliefs": [{"insight_ref": 1, "sources": [3]}]}
    assert agents._extraction_problems(ok, sources_of) == []
    bad = {"beliefs": [{"insight_ref": 1, "sources": [4]}, {"insight_ref": 7, "sources": [2]}]}
    assert agents._extraction_problems(bad, sources_of) == [
        "a belief from insight 1 cites entries [4], which are not among that insight's sources [2, 3]",
        "belief cites insight 7, not one of [1, 2]"]
