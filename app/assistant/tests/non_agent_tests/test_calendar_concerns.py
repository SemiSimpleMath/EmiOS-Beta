"""The day's beliefs become concerns (docs/design/concerns_as_state_2026-10-09.md, rule 7; 2026-10-09).

Owner, 2026-10-09: "maybe they should just become concerns. then really everything is a concern."; "an expiri of at
most 2 hours or something reasonable determined by the agent creating them, all day event might have expiri at
midnight."; "right day pass would only ever edit these concerns. question is then since they are concerns brain can
also edit them."

Pins: each placement is one concern, its identity its belief, day and time; it expires at its `until`, the next one
after its time (after midnight when the day runs past it), or at midnight for an all-day placement; one already past
is not raised; placing the day again updates the day pass's own fields and closes a concern no longer placed; one the
brain closed stays closed; nothing is raised before the cutover day; calendar concerns have no planner pass. Scratch
register; invented data only.
"""
from datetime import datetime, timedelta, timezone

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import calendar_concerns as K, concern_planner
from app.assistant.tests.concern_store_helpers import ScratchRegister
from app.assistant.utils.time_utils import get_local_timezone

DAY = "2026-10-12"
TZ = get_local_timezone()
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}


def _local(day, hour, minute=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=TZ).astimezone(timezone.utc)


def _line(key, time, until="", outcome=None):
    return {"belief_key": key, "time": time, "until": until, "statement": f"Belief {key}.",
            "outcome": outcome or f"{key} is done", "related": []}


def _plan(*lines, day=DAY):
    return {"day": day, "flags": [x for x in lines if x["time"] == "all day"],
            "slots": [{"lines": [x for x in lines if x["time"] != "all day"]}]}


def test_identity_and_expiry():
    assert K.concern_id(DAY, "B3", "9:00 PM") == K.concern_id(DAY, "B3", "9:00  pm")
    assert K.concern_id(DAY, "B3", "9:00 PM") != K.concern_id(DAY, "B3", "7:00 AM")
    assert K.expires_at(DAY, _line("B3", "9:00 PM", "11:00 PM")) == _local(DAY, 23)
    assert K.expires_at(DAY, _line("B4", "11:30 PM", "1:00 AM")) == _local("2026-10-13", 1), "past midnight"
    assert K.expires_at(DAY, _line("B5", "all day")) == _local("2026-10-13", 0), "midnight ending the day"


def test_a_placement_becomes_a_concern_and_one_already_past_does_not():
    now = _local(DAY, 20)
    [c] = K.from_plan(_plan(_line("B3", "9:00 PM", "11:00 PM"), _line("B4", "6:00 AM", "7:00 AM")), now)
    assert c["concern_id"] == K.concern_id(DAY, "B3", "9:00 PM") and c["title"] == c["done_when"] == "B3 is done"
    assert c["source"] == {"kind": "belief", "at": now.isoformat(), "ref": "belief:B3", "content": "Belief B3."}
    assert c["belief_refs"] == ["B3"] and c["calendar"] == {"day": DAY, "belief_key": "B3", "time": "9:00 PM"}
    assert c["expires_at"] == _local(DAY, 23).isoformat() and c["horizon"] == "today"


def test_the_day_pass_raises_updates_and_withdraws_its_own_and_a_closed_one_stays_closed(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    now = _local(DAY, 8)
    plan = _plan(_line("B3", "9:00 PM", "11:00 PM"), _line("B5", "all day"))
    assert K.apply(plan, now, connect=reg.connect) == {"raised": 2, "updated": 0, "withdrawn": 0}
    assert K.apply(plan, now, connect=reg.connect) == {"raised": 0, "updated": 0, "withdrawn": 0}, "once"
    ac = next(c for c in reg.read()["active"] if c["calendar"]["belief_key"] == "B3")
    assert ac["origin"] == "day_pass" and ac["attached_work"] == {}

    # the brain notes and resolves like any concern; the day pass never reopens what it closed
    register = reg.read()
    [flag] = [c for c in register["active"] if c["calendar"]["belief_key"] == "B5"]
    register["active"].remove(flag)
    register["resolved"].append({**flag, "resolution_reason": "someone else already did it"})
    reg.write(register)
    again = _plan(_line("B3", "9:00 PM", "11:00 PM", outcome="The house is at 70 for the night"), _line("B5", "all day"))
    assert K.apply(again, now + timedelta(hours=1), connect=reg.connect) == {"raised": 0, "updated": 1, "withdrawn": 0}
    after = reg.read()
    assert [c["title"] for c in after["active"]] == ["The house is at 70 for the night"]
    assert [c["concern_id"] for c in after["resolved"]] == [flag["concern_id"]], "closed stays closed"

    # a placement the day pass no longer makes closes its concern
    assert K.apply(_plan(day=DAY), now + timedelta(hours=2), connect=reg.connect)["withdrawn"] == 1
    assert reg.read()["active"] == []
    assert reg.read()["resolved"][-1]["resolution_reason"] == "the day pass no longer places it today"


def test_nothing_is_raised_before_the_cutover_day(tmp_path):
    reg = ScratchRegister(tmp_path).write(EMPTY)
    assert K.apply(_plan(_line("B3", "9:00 PM", "11:00 PM"), day="2026-10-09"), _local("2026-10-09", 8),
                   connect=reg.connect) == {"raised": 0, "updated": 0, "withdrawn": 0}
    assert reg.read()["active"] == []


def test_a_calendar_concern_has_no_planner_pass():
    now = _local(DAY, 8)
    [c] = K.from_plan(_plan(_line("B3", "9:00 PM", "11:00 PM")), now)
    matter = {"concern_id": "c-fine", "title": "Library fine", "notes": "n", "first_observed": now.isoformat()}
    assert [x["concern_id"] for _, x in concern_planner.stale({**EMPTY, "active": [c, matter]}, now)] == ["c-fine"]
