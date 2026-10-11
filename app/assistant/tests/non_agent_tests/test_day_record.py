"""The day's record, in shadow (subconscious/day_record.py; docs/design/day_record_2026-10-09.md).

Pins: the calendar's events enter by code, change with the calendar and are cancelled when gone from it; an event
whose end passed with nothing showing becomes unconfirmed (time is not evidence); the night and the time at and away
from the computer become milestones from the sleep and activity data; the model answers only with changes and milestones, each naming what shows it (relies_on, labels mapped to
objects and kept as shown_by relations) with its basis; a change with nothing to show for it, a label not given or
one written into words is malformed; a run repeats only when what it reads changed; the prompt shows labels and no
id. Scratch database; fake agent; invented data only.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import day_record as R
from app.assistant.subconscious import relations
from belief_engine.intake.store import sqlite_file

DAY = "2026-03-10"
NOW = datetime(2026, 3, 10, 19, 0, tzinfo=timezone.utc)        # 12:00 PM local (UTC-7)


def _ev(cid, title, start_h, end_h):
    at = lambda h: datetime(2026, 3, 10, h, 0, tzinfo=timezone.utc).isoformat()
    return {"calendar_item_id": cid, "title": title, "start_utc": at(start_h), "end_utc": at(end_h)}


CAL = [_ev("dogs", "Take out the dogs", 15, 16), _ev("school", "Late start at school", 16, 17)]


def _conn(tmp_path):
    conn = sqlite_file(tmp_path / "r.db")
    R.ensure_schema(conn)
    from app.assistant.subconscious import brain_inbox, brain_step
    brain_inbox.ensure_schema(conn)
    brain_step.ensure_schema(conn)
    return conn


def test_the_calendar_enters_changes_and_leaves_by_code(tmp_path):
    conn = _conn(tmp_path)
    assert R.sync_calendar(DAY, NOW, events=CAL[:2], connect=conn) == 2
    assert R.sync_calendar(DAY, NOW, events=CAL[:2], connect=conn) == 0, "nothing changed"
    moved = [{**CAL[0], "title": "Take out the dogs (late)"}]
    R.sync_calendar(DAY, NOW, events=moved, connect=conn)
    events, _ = R.records(DAY, connect=conn)
    by = {e["id"]: e for e in events}
    assert by["cal:dogs"]["title"] == "Take out the dogs (late)" and by["cal:dogs"]["state"] == "planned"
    assert by["cal:school"]["state"] == "cancelled" and by["cal:school"]["note"] == "removed from the calendar"


def test_time_passing_makes_unconfirmed_never_happened(tmp_path):
    conn = _conn(tmp_path)
    R.sync_calendar(DAY, NOW, events=CAL[:2], connect=conn)
    assert R.mark_passed(DAY, NOW, connect=conn) == 2           # both ended before noon local
    events, _ = R.records(DAY, connect=conn)
    assert {e["state"] for e in events} == {"unconfirmed"} and all(e["basis"] == "" for e in events)


def test_the_night_and_the_computer_time_become_milestones_by_code(tmp_path):
    """Owner, 2026-10-09: the brain's presence events are coarser than the tracker's activity data; the record reads
    the sleep pipeline's periods and the tracker's activity spans, rebuilt each pass."""
    conn = _conn(tmp_path)
    t = lambda h, m=0: datetime(2026, 3, 10, h, m, tzinfo=timezone.utc)
    sleep = [{"start": t(6, 20), "end": t(11, 20), "kind": "main_sleep"}]
    spans = [{"start_utc": t(14, 36), "end_utc": t(15, 8), "type": "Active", "ongoing": False},
             {"start_utc": t(15, 8), "end_utc": t(15, 40), "type": "AFK", "ongoing": False},
             {"start_utc": t(15, 40), "end_utc": NOW, "type": "Active", "ongoing": True}]
    current = R.time_milestones(DAY, NOW, sleep=sleep, spans=spans, connect=conn)
    R.time_milestones(DAY, NOW, sleep=sleep, spans=spans, connect=conn)
    _, milestones = R.records(DAY, connect=conn)
    assert [(m["origin"], m["basis"]) for m in milestones] == [("sleep", "inferred"), ("activity", "direct"),
                                                               ("activity", "direct")], "rebuilt, not doubled"
    assert milestones[0]["description"] == "asleep (main sleep) 11:20 PM to 4:20 AM, 5 h 0 min"
    assert milestones[2]["description"] == "away from the computer 8:08 AM to 8:40 AM, 32 min"
    assert current == {"words": "at the computer since 8:40 AM (3 h 20 min)"}, "the span still going is the state now"


def _inputs(conn):
    def gather(now):
        R.sync_calendar(DAY, now, events=CAL[:2], connect=conn)
        events, milestones = R.records(DAY, connect=conn)
        for e in events:
            e["time_is_now"] = False
        labels = {"K1": ["day_event", "cal:dogs"], "K2": ["day_event", "cal:school"],
                  "C1": ["concern", "c-sick"], "B1": ["brain_matter", "4"]}
        for e, k in zip(events, ("K1", "K2")):
            e["ref"], e["shown_by"] = k, []
        return {"day": DAY, "now": now.isoformat(), "current": None, "events": events, "milestones": milestones,
                "concerns": [{"ref": "C1", "concern_id": "c-sick", "title": "The daughter is home sick",
                              "subject": "the daughter", "what": "She has a cold.", "raised_today": None,
                              "today": [{"at": NOW.isoformat(), "text": "Still sick today; staying home."}]}],
                "matters": [], "labels": labels, "correction": None}
    return gather


ANSWER = {"changes": [{"event": "K2", "title": "", "start": "", "end": "", "state": "cancelled", "basis": "direct",
                       "relies_on": ["C1"], "note": "She is home sick today, so no late start."},
                      {"event": "new", "title": "Doctor's call", "start": "3:00 PM", "end": "", "state": "planned",
                       "basis": "direct", "relies_on": ["C1"], "note": "The concern names a call at 3."}],
          "milestones": [{"time": "11:30 AM", "description": "The owner said she is staying home.", "basis": "direct",
                          "relies_on": ["C1"]}],
          "day_theme": "A work day with the daughter home sick.", "current_status": "At work."}


def test_a_concern_changes_the_day_and_every_change_keeps_what_showed_it(tmp_path):
    """Owner, 2026-10-09: "[the daughter] is out sick -> leads to [her] late start at school being cancelled"."""
    conn = _conn(tmp_path)
    out = R.run_day_record(now_utc=NOW, connect=conn, gather_inputs=_inputs(conn), call=lambda p: ANSWER)
    assert out == {"day_record": {"changed": 1, "new": 1, "milestones": 1}}
    events, milestones = R.records(DAY, connect=conn)
    by = {e["title"]: e for e in events}
    assert by["Late start at school"]["state"] == "cancelled" and by["Late start at school"]["basis"] == "direct"
    assert relations.targets(("day_event", "cal:school"), "shown_by", connect=conn) == [("concern", "c-sick")]
    assert by["Doctor's call"]["start_utc"] == datetime(2026, 3, 10, 22, 0, tzinfo=timezone.utc).isoformat()
    assert milestones[0]["description"] == "The owner said she is staying home." and milestones[0]["origin"] == "model"
    assert R.run_day_record(now_utc=NOW, connect=conn, gather_inputs=_inputs(conn),
                            call=lambda p: pytest.fail("nothing read changed")) == {"day_record": "unchanged"}


@pytest.mark.parametrize("change,problem", [
    (lambda a: a["changes"][0].update(relies_on=[]), "relies_on names nothing"),
    (lambda a: a["changes"][0].update(relies_on=["C9"]), "['C9'] are not labels given"),
    (lambda a: a["changes"][0].update(event="C1"), "not an event given"),
    (lambda a: a["changes"][0].update(state="completed"), "state 'completed'"),
    (lambda a: a["changes"][0].update(basis="guess"), "basis 'guess'"),
    (lambda a: a["changes"][1].update(start=""), "a new event names its title and its start"),
    (lambda a: a["milestones"][0].update(relies_on=[]), "milestone 1: relies_on names nothing"),
])
def test_a_claim_with_nothing_to_show_for_it_is_malformed(change, problem):
    import copy
    labels = {"K1": ["day_event", "a"], "K2": ["day_event", "b"], "C1": ["concern", "c"]}
    answer = copy.deepcopy(ANSWER)
    change(answer)
    assert any(problem in p for p in R.problems(answer, labels))
    assert R.problems(ANSWER, labels) == []


def test_the_prompt_shows_labels_and_no_id(tmp_path):
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    conn = _conn(tmp_path)
    inputs = _inputs(conn)(NOW)
    user = _ENV.get_template("subconscious/day_record/prompts/user.j2").render(agent_input=inputs)
    assert "- [K2] Late start at school" in user and "### [C1] The daughter is home sick" in user
    assert "Still sick today; staying home." in user and "cal:" not in user and "c-sick" not in user
    system = _ENV.get_template("subconscious/day_record/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"}, resource_user_data={"first_name": "Sam"})
    assert "Time passing alone is never a reason to write happened" in system
