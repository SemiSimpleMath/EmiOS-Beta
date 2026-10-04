"""Handing a ready concern to dayflow, and the link that follows it.

Pins: a matter concern whose current plan (subconscious/concern_planner.py, made from its current brief)
says act_now becomes one intake item in the planner's inbox (past triage), carrying the concern, the brief
and the things that should be done; once per plan version; not while an earlier handoff is still in the
inbox, but work in progress does not hold it back (the plan decided it should be added to or corrected);
hold and no_action are not handed over, and a passed hold is planned again; work made from the item
carries the concern whatever the steward cites; the planner's answer is journalled on the concern.
Scratch register; the dayflow item store and work store are fakes. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import concern_brief, concern_handoff, concern_planner
from app.assistant.tests.concern_store_helpers import ScratchRegister

NOW = datetime.now(timezone.utc)
BRIEF = {"what": "The bake sale moved to Monday.", "why_it_matters": "Cookies are promised.",
         "known": [], "tried": "Nothing yet.", "owner_wishes": "Nothing said.", "depends_on_it": ["Thu reminder"],
         "open_questions": [], "recommendation": "Update the plans for Monday."}


# Where each attached work stands, as the work store would report it (work_objects/progress.py).
PROGRESS = {
    "work_1": {"work_id": "work_1", "state": "in_progress", "ended_at": None, "reason": None, "replanning": False,
               "done": [], "dropped": [], "unfinished": [], "to_come": [{"title": "Order the cake", "wait": {"kind": "ready"}}]},
    "work_a": {"work_id": "work_a", "state": "done", "ended_at": "2026-03-10T11:00:00+00:00", "reason": "judged complete",
               "replanning": False, "dropped": [], "unfinished": [], "to_come": [],
               "done": [{"title": "Ask", "verdict": "achieved", "at": "2026-03-10T10:30:00+00:00"}]},
}


@pytest.fixture(autouse=True)
def _work_store(monkeypatch):
    from app.assistant.subconscious import work_links
    monkeypatch.setattr(work_links, "progress", lambda work_id: PROGRESS[work_id])


def _with_brief(reg, readiness_by_id):
    """Each concern gets a current brief and a plan made from it: decision, things to do, why, hold."""
    register = reg.read()
    for c in register["active"]:
        r = readiness_by_id[c["concern_id"]]
        c["brief"] = {**BRIEF, "basis": concern_brief.basis(c, "active")}
        c["plan"] = {"decision": r["decision"], "todo": [r["task"]] if r.get("task") else [], "why": r["why"],
                     "hold_until": r.get("hold_until"), "basis": c["brief"]["basis"],
                     **({"hold_until_utc": r["hold_until_utc"]} if r.get("hold_until_utc") else {})}
    reg.write(register)


def _run(reg, *, items=None):
    written, pokes = [], []
    out = concern_handoff.run_handoffs(
        register_connect=reg.connect, now_utc=NOW,
        existing_items=lambda cid: [m for m in (items or []) + written if m["concern_id"] == cid],
        write_item=written.append, poke=lambda: pokes.append(1))
    return out, written, pokes


def _ready(reg, **readiness):
    _with_brief(reg, {c["concern_id"]: readiness for c in reg.read()["active"]})


def test_a_concern_ready_to_act_on_becomes_one_inbox_item(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [{"concern_id": "c-bake", "title": "PTSA bake sale",
                                                       "done_when": "cookies delivered", "notes": "n", "evidence": []}],
                                           "addressing": [], "resolved": [], "dormant": []})
    _ready(reg, decision="act_now", task="Move the bake-sale plans to Monday", why="The sale moved.")
    out, written, pokes = _run(reg)
    assert out == {"handed_over": 1, "handoff_skipped": 0} and pokes == [1]
    [item] = written
    assert item["item_id"].startswith("concern_handoff:c-bake:") and item["source_type"] == "concern"
    assert (item["state"], item["evaluator_pending"]) == ("artifact", True), "past triage, into the inbox"
    assert item["summary"] == "Move the bake-sale plans to Monday" and item["why_now"] == "The sale moved."
    assert "PTSA bake sale" in item["brief_text"] and "Thu reminder" in item["brief_text"]
    # The same brief version is not handed over twice.
    out, written2, _ = _run(reg, items=written)
    assert out["handed_over"] == 0 and written2 == []


def test_no_second_handoff_while_one_waits_but_active_work_does_not_hold_it_back(tmp_path):
    work = {"work_id": "work_1", "objective": "Order the cake for the 9th", "status": "active", "judgments": [],
            "ended": None, "attached_at": "2026-03-10T10:00:00+00:00"}
    reg = ScratchRegister(tmp_path).write({"active": [{"concern_id": "c-bake", "title": "t", "notes": "n", "evidence": [],
                                                       "attached_work": {"work_1": work}}],
                                           "addressing": [], "resolved": [], "dormant": []})
    _ready(reg, decision="act_now", task="Move the cake order to the 16th", why="The sale moved.")
    waiting = [{"concern_id": "c-bake", "item_id": "concern:c-bake:old", "state": "artifact", "evaluator_pending": True}]
    assert _run(reg, items=waiting)[0] == {"handed_over": 0, "handoff_skipped": 1}
    assert _run(reg)[0] == {"handed_over": 1, "handoff_skipped": 0}


def test_hold_and_no_action_stay_with_the_brain_and_a_passed_hold_is_planned_again(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [{"concern_id": "c-bake", "title": "t", "notes": "n", "evidence": []}],
                                           "addressing": [], "resolved": [], "dormant": []})
    _ready(reg, decision="hold", task="", why="w", hold_until="x",
           hold_until_utc=(NOW + timedelta(hours=1)).isoformat())
    assert _run(reg)[0]["handed_over"] == 0
    assert concern_planner.stale(reg.read(), now_utc=NOW) == []
    assert [c["concern_id"] for _, c in concern_planner.stale(reg.read(), now_utc=NOW + timedelta(hours=2))] == ["c-bake"]
    assert concern_brief.stale(reg.read(), now_utc=NOW + timedelta(hours=2)) == [], "the brief itself is still current"


def test_a_hold_needs_a_future_time():
    later = (NOW + timedelta(days=3)).astimezone().strftime("%Y-%m-%d %H:%M")
    assert concern_brief.hold_until_utc(later, NOW) > NOW
    with pytest.raises(ValueError, match="not in the future"):
        concern_brief.hold_until_utc("2020-01-01 09:00", NOW)


def test_work_made_from_the_item_carries_the_concern_whatever_the_steward_cites():
    from app.assistant.dayflow_orchestrator.work_intake import concern_refs_of, source_records
    item = {"id": "concern:c-bake:abc", "metadata": {"item_id": "concern:c-bake:abc", "source_type": "concern",
                                                     "summary": "Move the plans", "concern_id": "c-bake"}}
    sources = source_records([item], ["concern:c-bake:abc"])
    assert sources[0]["concern_id"] == "c-bake"
    assert concern_refs_of(sources) == ["concern:c-bake"]


def test_the_planners_answer_is_journalled_on_the_concern(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [{"concern_id": "c-bake", "title": "t", "notes": "n", "evidence": []}],
                                           "addressing": [], "resolved": [], "dormant": []})
    concern_handoff.record_intake_outcome({"concern_id": "c-bake", "item_id": "concern:c-bake:abc"},
                                          "reviewed it as no_action", "already handled by the school", connect=reg.connect)
    [c] = reg.read()["active"]
    assert "HANDOFF concern:c-bake:abc: the planner reviewed it as no_action: already handled by the school" in (
        c["reinforcement_notes"])


def test_citing_a_handoff_item_does_not_look_like_a_concern_ref():
    """The steward cites the item id in based_on, where `concern:` prefixes a concern reference."""
    assert not concern_handoff.item_id("c-bake", "abcdef123456789").startswith("concern:")


def test_the_brain_wakes_at_the_earliest_hold_of_a_current_plan(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [
        {"concern_id": "c-soon", "title": "t", "notes": "n", "evidence": []},
        {"concern_id": "c-late", "title": "t", "notes": "n", "evidence": []},
        {"concern_id": "c-none", "title": "t", "notes": "n", "evidence": []}],
        "addressing": [], "resolved": [], "dormant": []})
    hold = lambda h: {"decision": "hold", "task": "", "why": "w", "hold_until": "x",
                      "hold_until_utc": (NOW + timedelta(hours=h)).isoformat()}
    _with_brief(reg, {"c-soon": hold(1), "c-late": hold(5),
                      "c-none": {"decision": "no_action", "task": "", "why": "w", "hold_until": None}})
    assert concern_planner.next_hold_at(reg.read(), NOW) == NOW + timedelta(hours=1)
    assert concern_planner.next_hold_at(reg.read(), NOW + timedelta(hours=2)) == NOW + timedelta(hours=5)
    register = reg.read()
    register["active"][1]["brief"]["basis"] = "a newer brief"   # its plan is out of date: planned again
    reg.write(register)
    assert concern_planner.next_hold_at(reg.read(), NOW + timedelta(hours=2)) is None


def test_the_steward_sees_every_earlier_attempt_with_the_owners_replies(tmp_path):
    work = {"work_id": "work_a", "title": "Check in", "objective": "Check in on the bake sale plan",
            "attached_at": "2026-03-10T10:00:00+00:00", "status": "done",
            "judgments": [{"node_id": "ask", "title": "Ask", "verdict": "achieved", "next_step": "",
                           "outcome": "The owner will bake on Sunday.", "at": "2026-03-10T10:30:00+00:00",
                           "replies": [{"question": "Who bakes?", "user_text": "I will, Sunday"}]}],
            "ended": {"outcome": "done", "at": "2026-03-10T11:00:00+00:00", "reason": "judged complete"}}
    reg = ScratchRegister(tmp_path).write({"active": [{"concern_id": "c-bake", "title": "Bake sale", "notes": "n",
                                                       "evidence": [], "attached_work": {"work_a": work}}],
                                           "addressing": [], "resolved": [], "dormant": []})
    _ready(reg, decision="act_now", task="Confirm the cookie count", why="Monday is close", hold_until=None)
    _, [item], _ = _run(reg)
    text = item["brief_text"]
    assert "work on this concern so far:" in text
    from app.assistant.utils.time_utils import local_time_text
    assert (f"Check in on the bake sale plan (work_a; attached {local_time_text('2026-03-10T10:00:00+00:00')})\n"
            f"      Ended {local_time_text('2026-03-10T11:00:00+00:00')}: done, every step finished.\n"
            f"      Done:\n        - Ask (achieved {local_time_text('2026-03-10T10:30:00+00:00')})\n"
            "      judgments of its steps:") in text
    assert 'task "Ask" judged achieved' in text and "The owner will bake on Sunday." in text
    assert 'said: "I will, Sunday"' in text
