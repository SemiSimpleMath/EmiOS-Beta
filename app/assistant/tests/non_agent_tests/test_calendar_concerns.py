"""The day's beliefs become concerns (docs/design/concerns_as_state_2026-10-09.md, rule 7; 2026-10-09/10).

Owner, 2026-10-09: "maybe they should just become concerns. then really everything is a concern."; "an expiri of at
most 2 hours or something reasonable determined by the agent creating them, all day event might have expiri at
midnight."; "right day pass would only ever edit these concerns. question is then since they are concerns brain can
also edit them."; 2026-10-10: "we already had an agent that looks at all the beliefs and picks out the ones that
apply today".

Pins: the plan's lines still ahead are made into placements by an agent, one per thing the day holds; each placement
is one concern carrying every belief it rests on; it expires at its `until`, the next one after its time (after
midnight when the day runs past it), or at midnight for an all-day placement; a placement that continues one of
today's concerns keeps it (a moved time moves its expiry, unchanged words keep their time); an open concern no
placement continues is withdrawn and not offered again; one the brain closed stays closed; the answer is checked,
corrected once, then fails loud; nothing is raised before the cutover day; calendar concerns have no planner pass.
Scratch register; the agent is a fake; invented data only.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import calendar_concerns as K, concern_planner
from app.assistant.tests.concern_store_helpers import ScratchRegister
from app.assistant.utils.time_utils import get_local_timezone

DAY = "2026-10-12"
TZ = get_local_timezone()
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}
CONTEXT = {"day_facts": {"weekday": "Monday", "date": "October 12, 2026"}, "now_local": "Monday, October 12, 8 AM",
           "daily_context": "(the day)"}


def _local(day, hour, minute=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=TZ).astimezone(timezone.utc)


def _line(key, time, until="", outcome=None):
    return {"belief_key": key, "time": time, "until": until, "statement": f"Belief {key}.",
            "outcome": outcome or f"{key} is done", "related": []}


def _plan(*lines, day=DAY):
    return {"day": day, "flags": [x for x in lines if x["time"] == "all day"],
            "slots": [{"lines": [x for x in lines if x["time"] != "all day"]}]}


def _p(lines, time, until="", outcome="It is done", continues=""):
    return {"lines": lines, "continues": continues, "outcome": outcome, "time": time, "until": until}


class Agent:
    """A fake placements agent: answers in turn, and keeps what it was shown."""

    def __init__(self, *answers):
        self.answers, self.shown = list(answers), []

    def __call__(self, payload):
        self.shown.append(payload)
        return {"placements": self.answers.pop(0)}


def test_expiry():
    assert K.expires_at(DAY, _line("B3", "9:00 PM", "11:00 PM")) == _local(DAY, 23)
    assert K.expires_at(DAY, _line("B4", "11:30 PM", "1:00 AM")) == _local("2026-10-13", 1), "past midnight"
    assert K.expires_at(DAY, _line("B5", "all day")) == _local("2026-10-13", 0), "midnight ending the day"


def test_lines_about_one_thing_become_one_concern_with_every_belief(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    now = _local(DAY, 7)
    plan = _plan(_line("B1", "8:00 AM", "9:00 AM"), _line("B2", "8:00 AM", "8:30 AM"), _line("B0", "6:00 AM", "6:30 AM"))
    agent = Agent([_p(["L1", "L2"], "8:00 AM", "9:00 AM", "The dogs have had their morning walk")])
    assert K.apply(plan, now, context=CONTEXT, call=agent, connect=reg.connect) == {
        "raised": 1, "updated": 0, "withdrawn": 0}
    assert [x["statement"] for x in agent.shown[0]["lines"]] == ["Belief B1.", "Belief B2."], "a past line is not offered"
    [c] = reg.read()["active"]
    assert c["title"] == c["done_when"] == "The dogs have had their morning walk"
    assert c["belief_refs"] == ["B1", "B2"] and c["calendar"] == {"day": DAY, "time": "8:00 AM"}
    assert c["source"] == {"kind": "belief", "at": now.isoformat(), "ref": "belief:B1,belief:B2",
                           "content": "Belief B1.\nBelief B2."}
    assert c["expires_at"] == _local(DAY, 9).isoformat() and c["origin"] == "day_pass" and c["attached_work"] == {}


def test_a_moved_placement_keeps_its_concern_and_one_no_longer_placed_is_withdrawn(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    now = _local(DAY, 8)
    first = _plan(_line("B3", "9:30 PM", "11:00 PM"), _line("B5", "10:00 AM", "11:00 AM"))
    K.apply(first, now, context=CONTEXT, call=Agent([_p(["L1"], "9:30 PM", "11:00 PM", "The house is cool"),
                                                     _p(["L2"], "10:00 AM", "11:00 AM", "The lights are off")]),
            connect=reg.connect)
    ac = next(c for c in reg.read()["active"] if c["title"] == "The house is cool")

    again = _plan(_line("B3", "9:00 PM", "10:00 PM"))
    agent = Agent([_p(["L1"], "9:00 PM", "10:00 PM", "The house is cool", continues="P1")])
    assert K.apply(again, now + timedelta(hours=1), context=CONTEXT, call=agent, connect=reg.connect) == {
        "raised": 0, "updated": 1, "withdrawn": 1}
    assert [x["title"] for x in agent.shown[0]["concerns"]] == ["The house is cool", "The lights are off"]
    [moved] = reg.read()["active"]
    assert moved["concern_id"] == ac["concern_id"], "a moved time keeps its concern"
    assert moved["calendar"]["time"] == "9:00 PM" and moved["expires_at"] == _local(DAY, 22).isoformat()
    assert moved["source"]["at"] == ac["source"]["at"], "unchanged words keep the time they were first given"
    [gone] = reg.read()["resolved"]
    assert gone["title"] == "The lights are off" and gone["resolution_reason"] == "the day pass no longer places it today"

    third = Agent([_p(["L1"], "9:00 PM", "10:00 PM", "The house is cool", continues="P1")])
    K.apply(again, now + timedelta(hours=2), context=CONTEXT, call=third, connect=reg.connect)
    assert [x["title"] for x in third.shown[0]["concerns"]] == ["The house is cool"], "a withdrawn one is not offered"


def test_one_the_brain_closed_stays_closed(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    now = _local(DAY, 8)
    plan = _plan(_line("B5", "all day"))
    K.apply(plan, now, context=CONTEXT, call=Agent([_p(["L1"], "all day", outcome="Trash night")]),
            connect=reg.connect)
    register = reg.read()
    [flag] = register["active"]
    register["active"].remove(flag)
    register["resolved"].append({**flag, "resolution_reason": "someone else already did it"})
    reg.write(register)
    agent = Agent([_p(["L1"], "all day", outcome="Trash night", continues="P1")])
    assert K.apply(plan, now + timedelta(hours=1), context=CONTEXT, call=agent, connect=reg.connect) == {
        "raised": 0, "updated": 0, "withdrawn": 0}
    assert agent.shown[0]["concerns"][0]["state"] == "closed by the brain"
    assert reg.read()["active"] == [] and [c["concern_id"] for c in reg.read()["resolved"]] == [flag["concern_id"]]


def test_the_answer_is_corrected_once_then_fails_loud(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    now = _local(DAY, 8)
    plan = _plan(_line("B1", "8:00 AM", "9:00 AM"), _line("B2", "9:00 PM", "10:00 PM"))
    bad = [_p(["L1", "L1"], "8:00 AM", "9:00 AM", continues="P4")]
    agent = Agent(bad, [_p(["L1"], "8:00 AM", "9:00 AM"), _p(["L2"], "9:00 PM", "10:00 PM")])
    assert K.apply(plan, now, context=CONTEXT, call=agent, connect=reg.connect)["raised"] == 2
    correction = agent.shown[1]["correction"]
    assert "L1: put it in exactly one placement" in correction and "L2: in no placement" in correction
    assert "'P4' is not one of today's concerns" in correction
    with pytest.raises(ValueError, match="still invalid"):
        K.apply(_plan(_line("B9", "7:00 PM", "8:00 PM")), now, context=CONTEXT,
                call=Agent([_p(["L1"], "7 pm", "8:00 PM")], [_p(["L1"], "7 pm", "8:00 PM")]), connect=reg.connect)


def test_nothing_is_raised_before_the_cutover_day(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    agent = Agent()
    assert K.apply(_plan(_line("B3", "9:00 PM", "11:00 PM"), day="2026-10-09"), _local("2026-10-09", 8),
                   context=CONTEXT, call=agent, connect=reg.connect) == {"raised": 0, "updated": 0, "withdrawn": 0}
    assert reg.read()["active"] == [] and agent.shown == []


def test_a_calendar_concern_has_no_planner_pass():
    now = _local(DAY, 8)
    [c] = K.desired(DAY, [_p(["L1"], "9:00 PM", "11:00 PM")], [_line("B3", "9:00 PM", "11:00 PM")], [], now)
    matter = {"concern_id": "c-fine", "title": "Library fine", "notes": "n", "first_observed": now.isoformat()}
    assert [x["concern_id"] for _, x in concern_planner.stale({**EMPTY, "active": [c, matter]}, now)] == ["c-fine"]
