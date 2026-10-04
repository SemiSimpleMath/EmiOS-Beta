"""The concern planner: what a matter concern needs now, as things that should be done under the concern.

Pins: it runs for an open matter concern whose brief is current and has no plan made from that brief, or
whose plan's hold has passed; never for a standing concern or before the brief; a plan that failed on this
brief waits for a change. The decider's answer is checked (act_now lists things to do, hold has a future
time, only act_now lists things) and stored against the brief it was made from; a brief that changed
meanwhile refuses it. The bullets reach every reader of the concern. The brief decides readiness only for a
standing concern. Scratch register; the manager call is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import concern_brief, concern_planner, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister

NOW = datetime.now(timezone.utc)
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}
BRIEF = {"what": "The book club moved from the 9th to the 16th.", "why_it_matters": "Food and guests are booked.",
         "known": [], "tried": "Food ordered for the 9th.", "owner_wishes": "Nothing said.", "depends_on_it": [],
         "open_questions": [], "recommendation": "Move what was arranged for the 9th."}


def _matter(cid="c-club", **kw):
    return {"concern_id": cid, "title": "Book club meeting", "kind": "anticipated_need", "done_when": "the meeting happens",
            "notes": "n", "evidence": [], **kw}


def _briefed(concern, bucket="active"):
    concern["brief"] = {**BRIEF, "basis": concern_brief.basis(concern, bucket)}
    return concern


def _later(hours):
    return (NOW + timedelta(hours=hours)).astimezone().strftime("%Y-%m-%d %H:%M")


def test_it_plans_a_briefed_matter_concern_once_per_brief():
    c = _briefed(_matter())
    register = {**EMPTY, "active": [c]}
    assert [x["concern_id"] for _, x in concern_planner.stale(register, NOW)] == ["c-club"]
    c["plan"] = {"decision": "no_action", "todo": [], "why": "w", "basis": c["brief"]["basis"]}
    assert concern_planner.stale(register, NOW) == []
    c["plan"]["basis"] = "an older brief"
    assert len(concern_planner.stale(register, NOW)) == 1


def test_it_waits_for_the_brief_and_a_failed_plan_waits_for_a_change():
    unbriefed = _matter("c-new")
    stale_brief = _matter("c-old", brief={**BRIEF, "basis": "an older record"})
    failed = _briefed(_matter("c-fail"))
    failed["plan_error"] = {"basis": failed["brief"]["basis"], "error": "x"}
    assert concern_planner.stale({**EMPTY, "active": [unbriefed, stale_brief, failed]}, NOW) == []


def test_a_passed_hold_is_planned_again_and_wakes_the_brain():
    c = _briefed(_matter())
    c["plan"] = {"decision": "hold", "todo": [], "why": "w", "hold_until": "x", "basis": c["brief"]["basis"],
                 "hold_until_utc": (NOW + timedelta(hours=1)).isoformat()}
    register = {**EMPTY, "active": [c]}
    assert concern_planner.stale(register, NOW) == []
    assert concern_planner.next_hold_at(register, NOW) == NOW + timedelta(hours=1)
    assert len(concern_planner.stale(register, NOW + timedelta(hours=2))) == 1


@pytest.mark.parametrize("answer,problem", [
    ({"decision": "act_now", "todo": [], "why": "w"}, "needs at least one thing"),
    ({"decision": "no_action", "todo": ["x"], "why": "w"}, "only act_now does"),
    ({"decision": "hold", "todo": [], "why": "w", "hold_until": "2020-01-01 09:00"}, "future local"),
    ({"decision": "act_now", "todo": ["x"], "why": ""}, "why is empty"),
    ({"decision": "maybe", "todo": [], "why": "w"}, "is not one of"),
])
def test_the_deciders_answer_is_checked(answer, problem):
    assert any(problem in p for p in concern_planner.problems(answer, NOW))


def test_a_plan_is_stored_against_its_brief_and_its_bullets_reach_the_readers(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    seen = []

    def invoke(task, information):
        seen.append(task)
        return {"decision": "act_now", "todo": ["The caterer's order moves to the 16th or is cancelled",
                                                "The eight guests know the new date"], "why": "It moved."}
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect) == {"plans_written": 1, "plans_failed": 0}
    assert "Book club meeting" in seen[0] and "Food ordered for the 9th." in seen[0]
    [c] = reg.read()["active"]
    assert c["plan"]["todo"][1] == "The eight guests know the new date" and c["plan"]["basis"] == c["brief"]["basis"]
    assert concern_brief.stale(reg.read(), NOW) == [], "storing the plan does not make the brief stale"
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect)["plans_written"] == 0
    from app.assistant.subconscious.concern_handoff import _brief_text
    text = _brief_text(c, "active")
    assert "things that should be done:\n    - The caterer's order moves to the 16th or is cancelled" in text


def test_a_hold_is_stored_with_its_time_and_an_invalid_answer_is_recorded(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter("c-a")), _briefed(_matter("c-b"))]})

    def invoke(task, information):
        if "c-a" in task:
            return {"decision": "hold", "todo": [], "why": "The caterer answers by Friday.", "hold_until": _later(48)}
        return {"decision": "act_now", "todo": [], "why": "w"}
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect) == {"plans_written": 1, "plans_failed": 1}
    a, b = reg.read()["active"]
    assert a["plan"]["decision"] == "hold" and a["plan"]["hold_until_utc"]
    assert "needs at least one thing" in b["plan_error"]["error"] and "plan" not in b


def test_a_plan_for_a_brief_that_changed_meanwhile_is_refused(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    assert persist.set_concern_plan("c-club", "an older brief", plan={"decision": "no_action", "todo": [], "why": "w"},
                                    connect=reg.connect) is False
    assert "plan" not in reg.read()["active"][0]


def test_the_brief_decides_nothing():
    brief = {k: v for k, v in BRIEF.items()}
    assert concern_brief._problems(brief, {}) == []
