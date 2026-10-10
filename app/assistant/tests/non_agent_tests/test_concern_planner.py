"""The concern planner: what a concern needs now, as things that should be done under the concern.

Pins: it runs for an open concern with no plan made from its current record, or whose plan's hold has passed; a plan
that failed on this record waits for a change. concern_tasks reads the concern and its entries on the brain's list and
answers only the changes it proposes; the answer is checked (act_now changes an entry, only act_now adds or modifies,
changes name entries on the list, hold has a future time) and stored against the record it was made from, its
proposals with it; the list is not touched (2026-10-09: the list decider decides them, subconscious/day_tasks.py); a
record that changed meanwhile refuses it. Until 2026-10-09 it planned from a brief rewritten on every change. The plan's todo is the open entries as the proposals would leave them, and reaches every reader
of the concern. Scratch register and list; the manager call is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import brain_list, concern_planner, concern_view, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister
from belief_engine.intake.store import sqlite_file

NOW = datetime.now(timezone.utc)
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}


def _matter(cid="c-club", **kw):
    return {"concern_id": cid, "title": "Book club meeting", "kind": "anticipated_need", "done_when": "the meeting happens",
            "notes": "The book club moved from the 9th to the 16th. Food ordered for the 9th.",
            "first_observed": (NOW - timedelta(hours=3)).isoformat(), "evidence": [], **kw}


def _later(hours):
    return (NOW + timedelta(hours=hours)).astimezone().strftime("%Y-%m-%d %H:%M")


def _new(what, part="tell the owner", when="now", why="It moved."):
    return {"entry": "new", "action": "new", "what": what, "assistant_part": part, "when": when, "why": why}


def _decided(lst, changes, cid="c-club"):
    """The list decider's changes to a concern's entries (brain_list.apply_entries)."""
    return brain_list.apply_entries("2026-03-10", [{**c, "concern_id": cid, "relies_on": []} for c in changes],
                                    cause="decided", at=NOW, connect=lst)


def test_it_plans_a_concern_once_per_version_of_its_record():
    c = _matter()
    register = {**EMPTY, "active": [c]}
    assert [x["concern_id"] for _, x in concern_planner.stale(register, NOW)] == ["c-club"]
    c["plan"] = {"decision": "no_action", "todo": [], "why": "w", "basis": concern_view.basis(c, "active")}
    assert concern_planner.stale(register, NOW) == []
    c["summary"] = {"text": "s", "lines": 0, "through": c["first_observed"]}
    assert concern_planner.stale(register, NOW) == [], "a summary is not a change to the record"
    c["reinforcement_notes"] = "\n[2026-03-10T09:00:00+00:00] [brain] The caterer confirmed."
    assert len(concern_planner.stale(register, NOW)) == 1


def test_a_failed_plan_waits_for_a_change():
    failed = _matter("c-fail")
    failed["plan_error"] = {"basis": concern_view.basis(failed, "active"), "error": "x"}
    assert concern_planner.stale({**EMPTY, "active": [failed]}, NOW) == []


def test_a_passed_hold_is_planned_again_and_wakes_the_brain():
    c = _matter()
    c["plan"] = {"decision": "hold", "todo": [], "why": "w", "hold_until": "x", "basis": concern_view.basis(c, "active"),
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


def test_a_plan_is_stored_against_its_record_with_its_proposals_and_reaches_the_readers(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_matter()]})
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
    assert c["plan"]["basis"] == concern_view.basis(c, "active")
    assert [x["what"] for x in c["plan"]["proposals"]] == c["plan"]["todo"]
    assert [x["action"] for x in c["plan"]["proposals"]] == ["new", "new"]
    assert brain_list.entries(concern_id="c-club", connect=lst) == [], "the planner proposes; the list is untouched"
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect,
                                        list_connect=lst)["plans_written"] == 0
    from app.assistant.subconscious.concern_handoff import _brief_text
    text = _brief_text(c, "active")
    assert "things that should be done:\n    - The caterer's order moves to the 16th or is cancelled" in text


def test_a_hold_is_stored_with_its_time_and_an_invalid_answer_is_recorded(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_matter("c-a"), _matter("c-b")]})

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


def test_a_changed_plan_proposes_modifies_and_cancels_by_id(tmp_path):
    lst = sqlite_file(tmp_path / "list.db")
    _decided(lst, [_new("The guests know the date is the 9th"), _new("The food is ordered")])
    guests, food = brain_list.entries(concern_id="c-club", connect=lst)
    seen = []
    answer = {"decision": "act_now", "why": "It moved to the 16th.", "entries": [
        {"entry": guests["id"], "action": "modify", "what": "The guests know the date is the 16th",
         "assistant_part": "email the guests with the owner's go", "when": "now", "why": "The date moved."}]}
    plan = concern_planner.write_plan(_matter(), "active", list_connect=lst,
                                      invoke=lambda t, i: seen.append(i) or answer)
    assert f"[{guests['id']}] The guests know the date is the 9th" in seen[0]
    assert plan["todo"] == ["The guests know the date is the 16th", "The food is ordered"], "the food entry is kept"
    assert plan["proposals"][0]["entry"] == guests["id"] and plan["proposals"][0]["action"] == "modify"
    assert [c["change"] for c in brain_list.changes(guests["id"], connect=lst)] == ["created"], "proposed, not applied"


def test_a_plan_for_a_record_that_changed_meanwhile_is_refused(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_matter()]})
    assert persist.set_concern_plan("c-club", "an older record", plan={"decision": "no_action", "todo": [], "why": "w"},
                                    connect=reg.connect) is False
    assert "plan" not in reg.read()["active"][0]


def test_the_decider_sees_what_it_cancelled_and_why(tmp_path):
    """2026-10-06: the decider re-created a question it had cancelled the night before; it saw only open entries."""
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_matter()]})
    lst = sqlite_file(tmp_path / "list.db")
    _decided(lst, [_new("Guests told", part="ask the owner whether the guests were told")])
    [e] = brain_list.entries(concern_id="c-club", connect=lst)
    _decided(lst, [{"entry": e["id"], "action": "cancel", "why": "The evening has passed."}])
    seen = []
    concern_planner.run_planners(invoke=lambda task, info: seen.append(info) or {"decision": "no_action", "why": "w"},
                                 register_connect=reg.connect, list_connect=lst)
    assert seen[0].startswith("(nothing yet)")
    assert "Cancelled earlier" in seen[0] and "Guests told" in seen[0] and "The evening has passed." in seen[0]
