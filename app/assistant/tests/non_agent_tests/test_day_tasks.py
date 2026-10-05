"""The day's tasks: the brain decides today's calendar, in shadow (docs/design/brain_delegates_work_2026-10-04.md).

Pins: each placement is offered once (belief and time), labelled in the plan's order; the answer decides every
placement exactly once, a needed one names who acts, the assistant's part and when, and covers only name other
placements; an invalid answer gets one correction round and still invalid raises; a run is recorded, and repeated
only when something it read has changed; nothing is handed to dayflow; the comparison sets the newest decisions
beside what dayflow did with the same beliefs; the prompts render the rules, the steward's purpose, the
placements and the brain's situation, times local. Scratch database; fake agent; invented data only.
"""
from datetime import datetime, timezone

import pytest

from app.assistant.subconscious import day_tasks as D
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def _plan():
    line = lambda key, time, text: {"belief_key": key, "time": time, "statement": text,
                                    "related": [{"belief_key": "B9", "statement": "The owner likes it quiet."}]}
    return {"flags": [line("B1", "all day", "The owner sometimes starts the weekly report on Sunday.")],
            "slots": [{"lines": [line("B2", "6:00 PM", "Family dinner is at 6 PM."),
                                 line("B3", "9:00 PM", "The owner wants the AC at 70 at 9 PM.")]},
                      {"lines": [line("B2", "6:00 PM", "Family dinner is at 6 PM.")]}]}


def situation(chat="I'll start the report at 5", **changes):
    """A situation as subconscious/situation.gather returns it; invented data."""
    return {"now": NOW.isoformat(), "day": "2026-03-10", "weekday": "Tuesday",
            "key_dates": "- 2026-03-12 (2d away): Sam's birthday",
            "calendar_today": [{"title": "Work Hours", "start_local": "09:00 AM", "end_local": "04:30 PM",
                                "status": "scheduled"}],
            "calendar_ahead": "(nothing in the next 30 days)", "day_theme": "A work day", "current_status": "At the desk",
            "presence": {"is_afk": True, "status_label": "AWAY", "duration_minutes": 42.0, "start_time_local": "07:18 AM",
                         "most_recent_afk": None},
            "location": "Currently: Home", "weather": {"city": "Springfield", "temperature": 61, "condition": "Clear"},
            "health": "Slept poorly.",
            "chat": [{"id": "m1", "at": NOW.isoformat(), "speaker": "Owner", "text": chat}],
            "todos": [{"title": "Weekly report", "status": "needsAction", "due": ""}],
            "work": {"in_motion": [], "ended": []},
            "reminders": {"status": "available", "since": NOW.isoformat(), "through": NOW.isoformat(), "records": []},
            "tickets": [], "concerns": [{"title": "Sleep is poor", "severity": "medium"}], **changes}


def _inputs(chat="I'll start the report at 5"):
    return {"day": "2026-03-10", "now": NOW.isoformat(),
            "items": [{**it, "current": None} for it in D.placements(_plan())],
            "situation": situation(chat), "correction": None}


GOOD = {"decisions": [
    {"item": "I1", "decision": "not_needed", "who_acts": "", "assistant_part": "", "when": "", "covers": [],
     "why": "The owner said in chat he starts at 5."},
    {"item": "I2", "decision": "needed", "who_acts": "the assistant", "assistant_part": "tell the owner dinner is starting",
     "when": "6:00 PM", "covers": [], "why": "A heads-up on the boundary."},
    {"item": "I3", "decision": "needed", "who_acts": "the assistant", "assistant_part": "set the AC to 70",
     "when": "9:00 PM", "covers": [], "why": "A standing wish."}]}


def test_each_placement_is_offered_once_in_the_plans_order():
    items = D.placements(_plan())
    assert [(i["label"], i["belief_key"], i["time"]) for i in items] == [
        ("I1", "B1", "all day"), ("I2", "B2", "6:00 PM"), ("I3", "B3", "9:00 PM")]
    assert items[0]["related"] == ["The owner likes it quiet."]


@pytest.mark.parametrize("change,problem", [
    (lambda d: d["decisions"].pop(), "not decided: ['I3']"),
    (lambda d: d["decisions"].append(dict(d["decisions"][0])), "decided more than once: ['I1']"),
    (lambda d: d["decisions"][1].update(when=""), "I2: needed names when"),
    (lambda d: d["decisions"][1].update(covers=["I9"]), "I2: covers ['I9']"),
    (lambda d: d["decisions"][0].update(covers=["I2"]), "I1: only a needed item covers others"),
    (lambda d: d["decisions"][0].update(decision="maybe"), "I1: decision 'maybe'"),
])
def test_the_answer_must_decide_every_placement_once(change, problem):
    import copy
    data = copy.deepcopy(GOOD)
    change(data)
    assert any(problem in p for p in D.problems(data, ["I1", "I2", "I3"]))
    assert D.problems(GOOD, ["I1", "I2", "I3"]) == []


def test_a_run_is_recorded_and_repeated_only_when_its_inputs_change(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    calls = []
    call = lambda payload: calls.append(payload) or GOOD
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: _inputs(), call=call)
    assert out == {"day_tasks": "decided", "day_items": 3, "day_needed": 2,
                   "day_list_changes": {"created": 3, "modified": 0, "unchanged": 0, "cancelled": 0}}
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: _inputs(), call=call) == \
        {"day_tasks": "unchanged"}
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: _inputs("dinner moved to 7"), call=call)
    assert len(calls) == 2, "a changed chat is a new run; an unchanged day is not"
    newest = D.latest("2026-03-10", connect=conn)
    assert [(d["label"], d["belief_key"], d["decision"]) for d in newest["decisions"]] == [
        ("I1", "B1", "not_needed"), ("I2", "B2", "needed"), ("I3", "B3", "needed")]


def test_an_invalid_answer_is_corrected_once_then_refused(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    bad = {"decisions": GOOD["decisions"][:2]}
    answers = iter([bad, GOOD])
    seen = []
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: _inputs(),
                             call=lambda p: seen.append(p.get("correction")) or next(answers))
    assert out["day_tasks"] == "decided" and seen[0] is None and "not decided: ['I3']" in seen[1]
    with pytest.raises(ValueError, match="still invalid"):
        D.run_day_tasks(now_utc=NOW, connect=sqlite_file(tmp_path / "e.db"), gather_inputs=lambda now: _inputs(),
                           call=lambda p: bad)


def test_no_plan_or_no_placements_costs_nothing(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    never = lambda p: pytest.fail("no agent call without placements")
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: None, call=never) == \
        {"day_tasks": "no plan"}
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: {**_inputs(), "items": []},
                              call=never) == {"day_tasks": "no placements"}


def test_the_comparison_sets_the_brain_beside_dayflow(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "d.db")
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: _inputs(), call=lambda p: GOOD)
    monkeypatch.setattr(D, "dayflow_handling", lambda day: {"B1": ["all day: converted_to_work_object:work_x"]})
    rows = D.compare("2026-03-10", connect=conn)
    assert rows[0]["decision"] == "not_needed" and rows[0]["dayflow"] == ["all day: converted_to_work_object:work_x"]
    assert rows[2]["dayflow"] == []


def test_the_prompts_carry_the_rules_the_stewards_purpose_and_the_situation():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
           "resource_orchestrator_user_prefs": "Wellness nudges are separate cron reminders."}
    system = _ENV.get_template("subconscious/day_tasks/prompts/system.j2").render(**res)
    assert "Every piece of work names who acts: the assistant, Sam, or another person by name" in system
    assert "A HEADS-UP is the opposite and belongs exactly on the boundary" in system      # when_to_interrupt.j2
    assert "You are helping the user's day go well" in system                              # stewardship.j2
    assert "Wellness nudges are separate cron reminders." in system
    assert "away from the keyboard" in system
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(agent_input=_inputs())
    from app.assistant.utils.time_utils import local_time_text
    assert "[I1] All day: The owner sometimes starts the weekly report on Sunday." in user
    assert "    read with: The owner likes it quiet." in user
    assert "[I3] At 9:00 PM: The owner wants the AC at 70 at 9 PM." in user
    assert f"[{local_time_text(NOW.isoformat())}] Owner: I'll start the report at 5" in user
    assert "- Weekly report (needsAction)" in user and "- Sleep is poor (severity: medium)" in user
    assert "The owner is away from the keyboard, since 07:18 AM (42 minutes)." in user
    assert "- Work Hours 09:00 AM - 04:30 PM (scheduled)" in user and "Health today: Slept poorly." in user
    assert NOW.isoformat() not in user, "every time in the prompt is local"
