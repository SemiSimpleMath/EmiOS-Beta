"""The brain's list (docs/design/brain_delegates_work_2026-10-04.md, section 1).

Pins: an entry is changed in place, a changed entry recorded as modified (before, after, cause) and a changed reason
alone as no change. Until 2026-10-09 the day's placements had calendar entries keyed by code to their belief and day;
each placement is a concern now (test_calendar_concerns.py). The concern side's prompt carries its rules and the
concern's entries by id (the planner's handling is pinned in test_concern_planner.py). Scratch database; fake agents.
"""
from datetime import datetime, timedelta, timezone

from app.assistant.subconscious import brain_list as L
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def test_a_changed_entry_is_a_modification_of_the_same_entry(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    entry = {"entry": "new", "action": "new", "what": "The AC is at 70 for the night", "assistant_part": "set the AC to 70",
             "when": "9:00 PM", "why": "A standing wish.", "concern_id": "c-ac", "relies_on": []}
    assert L.apply_entries("2026-03-10", [entry], cause="run 1", at=NOW, connect=conn)["created"] == 1
    [ac] = L.entries(concern_id="c-ac", connect=conn)
    same = {**entry, "entry": ac["id"], "action": "modify"}
    assert L.apply_entries("2026-03-10", [{**same, "why": "Still a standing wish."}], cause="run 2", at=NOW,
                           connect=conn)["unchanged"] == 1, "a changed reason alone is no change"
    later = NOW + timedelta(minutes=5)
    assert L.apply_entries("2026-03-10", [{**same, "when": "8:30 PM", "why": "The owner asked for 8:30 today."}],
                           cause="run 3", at=later, connect=conn)["modified"] == 1
    [ac] = L.entries(concern_id="c-ac", connect=conn)
    assert (ac["when_local"], ac["status"]) == ("8:30 PM", "open")
    history = L.changes(ac["id"], connect=conn)
    assert [h["change"] for h in history] == ["created", "modified"]
    assert history[1]["before"]["when_local"] == "9:00 PM" and history[1]["after"]["when_local"] == "8:30 PM"
    assert history[1]["cause"] == "run 3"


# ── concerns ───────────────────────────────────────────────────────────────

def test_the_concern_tasks_prompt_carries_the_rules_and_the_entries():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
           "resource_assistant_capabilities": "", "resource_owner_standing_directives": ""}
    system = _ENV.get_template("subconscious/concern_tasks/prompts/system.j2").render(**res)
    assert "An entry you leave out stays as it is" in system
    assert "Work already in motion for this concern is not a reason to start more" in system  # concern_rules.j2
    assert "it cannot act physically, or make anyone act" in system                       # capabilities.j2
    assert "with its deadline and every hard constraint" in system
    entries = _ENV.get_template("shared/brain/concern_entries.j2").render(entries=[
        {"id": "con:a", "what": "the owner knows", "assistant_part": "tell him", "when_local": "9:00 AM",
         "status": "open", "updated_at": NOW.isoformat()}])
    assert entries.startswith("- [con:a] the owner knows | the assistant: tell him | when: 9:00 AM | open, decided")
