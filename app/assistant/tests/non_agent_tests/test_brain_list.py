"""The brain's list, in shadow (docs/design/brain_delegates_work_2026-10-04.md, section 1).

Pins: a calendar placement's entry is keyed by code and changed in place, a changed decision recorded as modified
(before, after, cause) and a changed reason alone as no change; a placement dropped from the day is cancelled;
covers become entry ids; the day decider sees each placement's current entry, and the entries do not count as a
change of input. A concern's entries are kept, modified or cancelled by id and new ones added; the shadow concern
decider answers every entry once (one correction round), every concern is attempted and failures raised after;
the live planner hands over what it looked up without changing its own result. Scratch database; fake agents.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_list as L
from app.assistant.subconscious import concern_list_shadow as S
from app.assistant.subconscious import day_tasks as D
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
ITEMS = [{"label": "I1", "belief_key": "B3", "time": "9:00 PM", "statement": "The owner wants the AC at 70 at 9 PM."},
         {"label": "I2", "belief_key": "B4", "time": "9:00 PM", "statement": "The AC runs cool at night."}]


def _decision(when="9:00 PM", why="A standing wish."):
    return [{"item": "I1", "decision": "needed", "who_acts": "the assistant", "assistant_part": "set the AC to 70",
             "when": when, "covers": ["I2"], "why": why},
            {"item": "I2", "decision": "not_needed", "who_acts": "", "assistant_part": "", "when": "", "covers": [],
             "why": "Covered by I1."}]


def test_a_changed_decision_is_a_modification_of_the_same_entry(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    assert L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW, connect=conn) == \
        {"created": 2, "modified": 0, "unchanged": 0, "cancelled": 0}
    assert L.apply_calendar("2026-03-10", ITEMS, _decision(why="Still a standing wish."), cause="run 2",
                            at=NOW, connect=conn)["unchanged"] == 2, "a changed reason alone is no change"
    later = NOW + timedelta(minutes=5)
    assert L.apply_calendar("2026-03-10", ITEMS, _decision("8:30 PM", "The owner asked for 8:30 today."),
                            cause="run 3", at=later, connect=conn)["modified"] == 1
    ac = L.calendar_id("2026-03-10", "B3", "9:00 PM")
    [entry] = [e for e in L.entries(day="2026-03-10", connect=conn) if e["id"] == ac]
    assert (entry["when_local"], entry["status"], entry["covers"]) == \
        ("8:30 PM", "open", [L.calendar_id("2026-03-10", "B4", "9:00 PM")])
    history = L.changes(ac, connect=conn)
    assert [h["change"] for h in history] == ["created", "modified"]
    assert history[1]["before"]["when_local"] == "9:00 PM" and history[1]["after"]["when_local"] == "8:30 PM"
    assert history[1]["cause"] == "run 3"


def test_a_placement_dropped_from_the_day_is_cancelled(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW, connect=conn)
    one = [{**_decision()[0], "covers": []}]
    assert L.apply_calendar("2026-03-10", ITEMS[:1], one, cause="run 2", at=NOW, connect=conn)["cancelled"] == 1
    gone = L.calendar_id("2026-03-10", "B4", "9:00 PM")
    assert [e["id"] for e in L.entries(day="2026-03-10", connect=conn)] == [L.calendar_id("2026-03-10", "B3", "9:00 PM")]
    assert L.changes(gone, connect=conn)[-1]["change"] == "cancelled"


def test_the_days_tasks_see_each_placements_entry_and_entries_are_not_a_change_of_input(tmp_path):
    conn = sqlite_file(tmp_path / "l.db")
    L.apply_calendar("2026-03-10", ITEMS, _decision(), cause="run 1", at=NOW, connect=conn)
    seen = D.with_current("2026-03-10", ITEMS, connect=conn)
    assert seen[0]["current"]["when"] == "9:00 PM" and seen[1]["current"]["decision"] == "not_needed"
    from app.assistant.tests.non_agent_tests.test_day_tasks import situation
    base = {"day": "2026-03-10", "now": NOW.isoformat(), "situation": situation()}
    assert D.fingerprint({**base, "items": ITEMS}) == D.fingerprint({**base, "items": seen})
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={**base, "items": [{**it, "related": []} for it in seen], "correction": None})
    assert "on the list now: needed: the assistant: set the AC to 70, 9:00 PM" in user
    assert "on the list now: not needed: Covered by I1." in user


# ── concerns ───────────────────────────────────────────────────────────────

CONCERN = {"concern_id": "c-ring-1", "title": "Doorbell battery is low", "done_when": "charged", "evidence": [],
           "attached_work": {}, "reinforcement_notes": ""}


def _job():
    return {"concern": CONCERN, "bucket": "active", "looked_up": ["Ring emailed: battery low"]}


def test_a_concerns_entries_are_kept_modified_cancelled_and_added_by_id(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "l.db")
    monkeypatch.setattr("app.assistant.subconscious.concern_planner.concern_text", lambda c, b: "the concern")
    first = {"entries": [{"entry": "new", "action": "new", "what": "the owner knows the battery is low",
                          "who_acts": "the owner", "assistant_part": "tell the owner to recharge it", "when": "9:00 AM",
                          "why": "Ring sent a low-battery notice."}]}
    seen = []
    S.decide(_job(), now=NOW, call=lambda p: seen.append(p) or first, connect=conn)
    [entry] = L.entries(concern_id="c-ring-1", connect=conn)
    assert entry["id"].startswith("con:") and entry["status"] == "open" and seen[0]["looked_up"] == ["Ring emailed: battery low"]
    second = {"entries": [{"entry": entry["id"], "action": "cancel", "what": "", "who_acts": "", "assistant_part": "",
                           "when": "", "why": "The owner said it is charged."}]}
    S.decide(_job(), now=NOW, call=lambda p: seen.append(p) or second, connect=conn)
    assert seen[1]["entries"][0]["id"] == entry["id"], "the decider sees the entry it decides, by id"
    assert L.entries(concern_id="c-ring-1", connect=conn) == []
    assert [c["change"] for c in L.changes(entry["id"], connect=conn)] == ["created", "cancelled"]


@pytest.mark.parametrize("answer,problem", [
    ({"entries": []}, "not answered: ['con:a']"),
    ({"entries": [{"entry": "con:a", "action": "keep", "why": "fine"}] * 2}, "answered more than once"),
    ({"entries": [{"entry": "con:a", "action": "modify", "what": "x", "who_acts": "", "assistant_part": "y", "when": "z",
                   "why": "w"}]}, "con:a: modify names who_acts"),
    ({"entries": [{"entry": "con:zz", "action": "keep", "why": "w"}]}, "'con:zz' is not an entry on the list"),
    ({"entries": [{"entry": "con:a", "action": "keep", "why": "w"},
                  {"entry": "con:b", "action": "new", "what": "x", "who_acts": "a", "assistant_part": "b", "when": "c",
                   "why": "w"}]}, 'a new entry is written as entry "new"'),
])
def test_the_concern_answer_must_answer_every_entry_once(answer, problem):
    assert any(problem in p for p in S.problems(answer, ["con:a"]))


def test_every_concern_is_attempted_then_failures_are_raised(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "l.db")
    monkeypatch.setattr("app.assistant.subconscious.concern_planner.concern_text", lambda c, b: "the concern")
    other = {**CONCERN, "concern_id": "c-other"}
    calls = []
    good = {"entries": []}
    with pytest.raises(RuntimeError, match="1 of 2"):
        S.run_concern_shadow([{**_job()}, {"concern": other, "bucket": "active", "looked_up": []}], now_utc=NOW,
                             connect=conn, call=lambda p: calls.append(p["concern"]) or ({"entries": "bad"}
                                                                                        if len(calls) <= 2 else good))
    assert len(calls) == 3, "the first concern's two refused answers, then the second concern"
    assert S.run_concern_shadow([], connect=conn) == {"list_concerns": 0}


def test_the_live_planner_hands_over_what_it_looked_up_and_its_result_is_unchanged(tmp_path):
    from app.assistant.subconscious import concern_brief, concern_planner
    from app.assistant.tests.concern_store_helpers import ScratchRegister
    reg = ScratchRegister(tmp_path).write({"active": [{**CONCERN, "notes": "n"}], "addressing": [], "resolved": [],
                                           "dormant": []})
    register = reg.read()
    c = register["active"][0]
    c["brief"] = {"what": "w", "why_it_matters": "m", "known": [], "tried": "t", "owner_wishes": "o",
                  "depends_on_it": [], "open_questions": [], "recommendation": "r",
                  "basis": concern_brief.basis(c, "active")}
    reg.write(register)
    invoke = lambda task, info: {"decision": "no_action", "todo": [], "why": "handled", "_looked_up": ["a lookup"]}
    shadow = []
    assert concern_planner.run_planners(invoke=invoke, register_connect=reg.connect, shadow=shadow) == \
        {"plans_written": 1, "plans_failed": 0}
    assert [(j["concern"]["concern_id"], j["looked_up"]) for j in shadow] == [("c-ring-1", ["a lookup"])]
    assert "_looked_up" not in reg.read()["active"][0]["plan"], "the lookups are not stored in the plan"


def test_the_shadow_concern_prompt_carries_the_rules_and_the_entries():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
           "resource_orchestrator_user_prefs": ""}
    system = _ENV.get_template("subconscious/concern_list_decider/prompts/system.j2").render(**res)
    assert "Every piece of work names who acts" in system                          # who_acts.j2
    assert "Work already in motion for this concern is not a reason to start more" in system  # concern_rules.j2
    assert "Keep each one as it is unless something you read" in system           # keep_unless_changed.j2
    user = _ENV.get_template("subconscious/concern_list_decider/prompts/user.j2").render(agent_input={
        "now": NOW.isoformat(), "concern": "THE RECORD", "looked_up": ["Ring emailed"], "correction": None,
        "entries": [{"id": "con:a", "what": "the owner knows", "who_acts": "the owner", "assistant_part": "tell him",
                     "when_local": "9:00 AM", "why": "notice"}]})
    assert "THE RECORD" in user and "- Ring emailed" in user
    assert "[con:a] the owner knows | who acts: the owner | the assistant: tell him | when: 9:00 AM" in user
