"""The concern planner: what a matter concern needs now, as things that should be done under the concern.

Pins: it runs for an open matter concern whose brief is current and has no plan made from that brief, or
whose plan's hold has passed; never for a standing concern or before the brief; a plan that failed on this
brief waits for a change. concern_tasks reads the concern and its entries on the brain's list and answers only
the changes; the answer is checked (act_now changes an entry, only act_now adds or modifies, changes name entries
on the list, hold has a future time) and stored against the brief it was made from, its changes applied to the
list; a brief that changed meanwhile refuses it. The plan's todo is the open entries, and reaches every reader of
the concern. Scratch register and list; the manager call is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import brain_list, concern_brief, concern_planner, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister
from belief_engine.intake.store import sqlite_file

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


def _new(what, part="tell the owner", when="now", why="It moved."):
    return {"entry": "new", "action": "new", "what": what, "assistant_part": part, "when": when, "why": why}


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


CANCEL_A = {"entry": "con:a", "action": "cancel", "what": "", "assistant_part": "", "when": "", "why": "settled"}


@pytest.mark.parametrize("answer,problem", [
    ({"decision": "act_now", "entries": [], "why": "w"}, "needs at least one new or modified entry"),
    ({"decision": "no_action", "entries": [_new("x")], "why": "w"}, "only act_now does"),
    ({"decision": "hold", "entries": [], "why": "w", "hold_until": "2020-01-01 09:00"}, "future local"),
    ({"decision": "act_now", "entries": [_new("x")], "why": ""}, "why is empty"),
    ({"decision": "maybe", "entries": [], "why": "w"}, "is not one of"),
    ({"decision": "act_now", "entries": [{**_new("x"), "entry": "con:zz", "action": "modify"}], "why": "w"},
     "'con:zz' is not an entry on the list"),
    ({"decision": "act_now", "entries": [{**_new("x"), "entry": "con:a"}], "why": "w"}, 'written as entry "new"'),
    ({"decision": "act_now", "entries": [{**_new("x"), "when": ""}], "why": "w"}, "new names when"),
    ({"decision": "no_action", "entries": [CANCEL_A, CANCEL_A], "why": "w"}, "changed more than once"),
])
def test_the_answer_is_checked(answer, problem):
    assert any(problem in p for p in concern_planner.problems(answer, NOW, ["con:a"]))
    cancel = {"decision": "no_action", "why": "w", "entries": [CANCEL_A]}
    assert concern_planner.problems(cancel, NOW, ["con:a"]) == [], "a cancel comes with any decision"


def test_a_plan_is_stored_against_its_brief_its_entries_go_on_the_list_and_reach_the_readers(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    lst = sqlite_file(tmp_path / "list.db")
    seen = []

    def invoke(task, information):
        seen.append((task, information))
        return {"decision": "act_now", "why": "It moved.", "entries": [
            _new("The caterer's order moves to the 16th or is cancelled", "ask the owner whether to move it"),
            _new("The eight guests know the new date by September 9", "email the guests with the owner's go")]}
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect, list_connect=lst) == \
        {"plans_written": 1, "plans_failed": 0}
    assert "Book club meeting" in seen[0][0] and "Food ordered for the 9th." in seen[0][0]
    assert seen[0][1] == "(nothing yet)", "the decider reads the concern's entries now"
    [c] = reg.read()["active"]
    assert c["plan"]["todo"][1] == "The eight guests know the new date by September 9"
    assert c["plan"]["basis"] == c["brief"]["basis"]
    entries = brain_list.entries(concern_id="c-club", connect=lst)
    assert [e["what"] for e in entries] == c["plan"]["todo"] and all(e["id"].startswith("con:") for e in entries)
    assert concern_brief.stale(reg.read(), NOW) == [], "storing the plan does not make the brief stale"
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect,
                                        list_connect=lst)["plans_written"] == 0
    from app.assistant.subconscious.concern_handoff import _brief_text
    text = _brief_text(c, "active")
    assert "things that should be done:\n    - The caterer's order moves to the 16th or is cancelled" in text


def test_a_hold_is_stored_with_its_time_and_an_invalid_answer_is_recorded(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter("c-a")), _briefed(_matter("c-b"))]})

    def invoke(task, information):
        if "c-a" in task:
            return {"decision": "hold", "entries": [], "why": "The caterer answers by Friday.", "hold_until": _later(48)}
        return {"decision": "act_now", "entries": [], "why": "w"}
    lst = sqlite_file(tmp_path / "list.db")
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect, list_connect=lst) == \
        {"plans_written": 1, "plans_failed": 1}
    a, b = reg.read()["active"]
    assert a["plan"]["decision"] == "hold" and a["plan"]["hold_until_utc"]
    assert "needs at least one new or modified entry" in b["plan_error"]["error"] and "plan" not in b


def test_a_changed_plan_keeps_modifies_and_cancels_entries_by_id(tmp_path):
    lst = sqlite_file(tmp_path / "list.db")
    brain_list.apply_concern("c-club", [_new("The guests know the date is the 9th"), _new("The food is ordered")],
                             cause="first", at=NOW, connect=lst)
    guests, food = brain_list.entries(concern_id="c-club", connect=lst)
    seen = []
    answer = {"decision": "act_now", "why": "It moved to the 16th.", "entries": [
        {"entry": guests["id"], "action": "modify", "what": "The guests know the date is the 16th",
         "assistant_part": "email the guests with the owner's go", "when": "now", "why": "The date moved."}]}
    plan, changes = concern_planner.write_plan(_briefed(_matter()), "active", list_connect=lst,
                                               invoke=lambda t, i: seen.append(i) or answer)
    assert f"[{guests['id']}] The guests know the date is the 9th" in seen[0]
    assert plan["todo"] == ["The guests know the date is the 16th", "The food is ordered"], "the food entry is kept"
    brain_list.apply_concern("c-club", changes, cause="second", at=NOW, connect=lst)
    assert [c["change"] for c in brain_list.changes(guests["id"], connect=lst)] == ["created", "modified"]
    assert [c["change"] for c in brain_list.changes(food["id"], connect=lst)] == ["created"]


def test_a_plan_for_a_brief_that_changed_meanwhile_is_refused(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    assert persist.set_concern_plan("c-club", "an older brief", plan={"decision": "no_action", "todo": [], "why": "w"},
                                    connect=reg.connect) is False
    assert "plan" not in reg.read()["active"][0]


def test_the_brief_decides_nothing():
    brief = {k: v for k, v in BRIEF.items()}
    assert concern_brief._problems(brief, {}) == []


def test_a_plan_whose_entries_fail_is_not_stored_and_is_planned_again(tmp_path, monkeypatch):
    """Until 2026-10-05 the plan was stored first: a failure applying its entries left a current plan whose entries
    never reached the list, and nothing planned it again."""
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    lst = sqlite_file(tmp_path / "list.db")
    invoke = lambda task, information: {"decision": "act_now", "why": "It moved.", "entries": [_new("Guests told")]}

    def broken(*a, **k):
        raise OSError("list unavailable")
    real = brain_list.apply_concern
    monkeypatch.setattr(brain_list, "apply_concern", broken)
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect, list_connect=lst) == \
        {"plans_written": 0, "plans_failed": 1}
    [c] = reg.read()["active"]
    assert "plan" not in c and "plan_error" not in c
    monkeypatch.setattr(brain_list, "apply_concern", real)
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect,
                                        list_connect=lst)["plans_written"] == 1
    assert [e["what"] for e in brain_list.entries(concern_id="c-club", connect=lst)] == ["Guests told"]


def test_the_decider_sees_what_it_cancelled_and_why(tmp_path):
    """2026-10-06: the decider re-created a question it had cancelled the night before; it saw only open entries."""
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_briefed(_matter())]})
    lst = sqlite_file(tmp_path / "list.db")
    brain_list.apply_concern("c-club", [_new("Guests told", part="ask the owner whether the guests were told")],
                             cause="plan", at=NOW, connect=lst)
    [e] = brain_list.entries(concern_id="c-club", connect=lst)
    brain_list.apply_concern("c-club", [{"entry": e["id"], "action": "cancel", "why": "The evening has passed."}],
                             cause="plan", at=NOW, connect=lst)
    seen = []
    concern_planner.run_planners(invoke=lambda task, info: seen.append(info) or {"decision": "no_action", "why": "w"},
                                 register_connect=reg.connect, list_connect=lst)
    assert seen[0].startswith("(nothing yet)")
    assert "Cancelled earlier" in seen[0] and "Guests told" in seen[0] and "The evening has passed." in seen[0]
