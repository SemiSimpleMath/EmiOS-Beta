"""The brain's list (docs/design/brain_delegates_work_2026-10-04.md, section 1).

Pins: a calendar entry is keyed by code to its belief and day and changed in place, a changed decision recorded as
modified (before, after, cause) and a changed reason alone as no change; an entry left undecided is kept, and so is one
whose placement left the plan (only a decision ends an entry); covers become entry ids; the day's tasks see each placement's current
entry, and the entries do not count as a change of input. The concern side's prompt carries its rules and the
concern's entries by id (the planner's handling is pinned in test_concern_planner.py). Scratch database; fake agents.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_list as L
from app.assistant.subconscious import day_tasks as D
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
ITEMS = [{"label": "I1", "belief_key": "B3", "times": ["9:00 PM"], "statement": "The owner wants the AC at 70 at 9 PM."},
         {"label": "I2", "belief_key": "B4", "times": ["9:00 PM"], "statement": "The AC runs cool at night."}]


def _decision(when="9:00 PM", why="A standing wish."):
    return [{"item": "I1", "decision": "needed", "assistant_part": "set the AC to 70",
             "when": when, "covers": ["I2"], "why": why, "new_since": []},
            {"item": "I2", "decision": "not_needed", "assistant_part": "", "when": "", "covers": [],
             "why": "Covered by I1.", "new_since": []}]


def test_a_changed_decision_is_a_modification_of_the_same_entry(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    assert L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW,
                            connect=conn) == {"created": 2, "modified": 0, "unchanged": 0, "kept": 0}
    assert L.apply_calendar("2026-03-10", ITEMS, _decision(why="Still a standing wish."),
                            cause="run 2", at=NOW, connect=conn)["unchanged"] == 2, "a changed reason alone is no change"
    later = NOW + timedelta(minutes=5)
    assert L.apply_calendar("2026-03-10", ITEMS, _decision("8:30 PM", "The owner asked for 8:30 today.")[:1],
                            cause="run 3", at=later, connect=conn) == \
        {"created": 0, "modified": 1, "unchanged": 0, "kept": 1}, "the undecided entry is kept"
    ac = L.calendar_id("2026-03-10", "B3")
    [entry] = [e for e in L.entries(day="2026-03-10", connect=conn) if e["id"] == ac]
    assert (entry["when_local"], entry["status"], entry["covers"]) == \
        ("8:30 PM", "open", [L.calendar_id("2026-03-10", "B4")])
    history = L.changes(ac, connect=conn)
    assert [h["change"] for h in history] == ["created", "modified"]
    assert history[1]["before"]["when_local"] == "9:00 PM" and history[1]["after"]["when_local"] == "8:30 PM"
    assert history[1]["cause"] == "run 3"


def test_a_placement_dropped_from_the_day_keeps_its_entry(tmp_path):
    """The day pass re-places the day as its inputs change; a belief that drops out of one run and returns in the
    next is not the brain's decision to stop its work (2026-10-05)."""
    conn = sqlite_file(tmp_path / "l.db")
    L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW, connect=conn)
    assert L.apply_calendar("2026-03-10", ITEMS[:1], [], cause="run 2", at=NOW, connect=conn) == \
        {"created": 0, "modified": 0, "unchanged": 0, "kept": 1}
    assert [e["id"] for e in L.entries(day="2026-03-10", connect=conn)] == \
        [L.calendar_id("2026-03-10", "B3"), L.calendar_id("2026-03-10", "B4")]


def test_the_days_tasks_see_each_placements_entry_and_entries_are_not_a_change_of_input(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW, connect=conn)
    seen = D.with_current("2026-03-10", ITEMS, connect=conn)
    assert seen[0]["current"]["when"] == "9:00 PM" and seen[1]["current"]["decision"] == "not_needed"
    from app.assistant.tests.non_agent_tests.test_day_tasks import situation
    base = {"day": "2026-03-10", "now": NOW.isoformat(), "situation": situation(), "matters": [], "own": []}
    assert D.fingerprint({**base, "items": ITEMS}) == D.fingerprint({**base, "items": seen})
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={**base, "items": [{**it, "related": []} for it in seen], "correction": None})
    assert ": needed: set the AC to 70, 9:00 PM" in user and ": not needed: Covered by I1." in user


# ── concerns ───────────────────────────────────────────────────────────────

def test_the_concern_tasks_prompt_carries_the_rules_and_the_entries():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"}}
    system = _ENV.get_template("subconscious/concern_tasks/prompts/system.j2").render(**res)
    assert "An entry you leave out stays as it is" in system
    assert "Work already in motion for this concern is not a reason to start more" in system  # concern_rules.j2
    assert "it cannot act physically, or make anyone act" in system                       # capabilities.j2
    assert "with its deadline and every hard constraint" in system
    entries = _ENV.get_template("shared/brain/concern_entries.j2").render(entries=[
        {"id": "con:a", "what": "the owner knows", "assistant_part": "tell him", "when_local": "9:00 AM",
         "status": "open", "updated_at": NOW.isoformat()}])
    assert entries.startswith("- [con:a] the owner knows | the assistant: tell him | when: 9:00 AM | open, decided")
