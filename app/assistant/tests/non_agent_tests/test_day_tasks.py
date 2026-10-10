"""The day's tasks: the brain's one list decider (docs/design/hub_and_spokes_2026-10-09.md).

Pins: it decides every entry on the list, a concern's included, and since 2026-10-09 the day's calendar arrives as
concerns (subconscious/calendar_concerns.py), so a calendar concern's entry is a concern's entry; a run is recorded,
and repeated only when a stage before it changed (a concern's record or plan), never on chat or the rest of the
situation alone, and since 2026-10-10 when work a decision relied on ended (a cancellation keeps what it relied on
too); a concern planner's proposal reaches it by label; the prompts render the rules, the labelled objects
and the brain's situation, times local. The prompt is a projection (2026-10-08): every object shows a label of the
call, never its id; the answer refers only by labels in relies_on, concern and entry, code maps them back and keeps
what an entry relies on as relations, shown again on the next run. Scratch database; fake agent; invented data only.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_list as L
from app.assistant.subconscious import day_tasks as D
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
DAY = "2026-03-10"


def situation(chat="I'll start the report at 5", **changes):
    """A situation as subconscious/situation.gather returns it; invented data. Its first concern is one of the day's
    calendar concerns (a placed belief)."""
    return {"now": NOW.isoformat(), "day": DAY, "weekday": "Tuesday",
            "key_dates": "- 2026-03-12 (2d away): Sam's birthday",
            "calendar_today": [{"title": "Work Hours", "start_local": "09:00 AM", "end_local": "04:30 PM",
                                "status": "scheduled"}],
            "calendar_ahead": [{"anchor": None, "text": "(nothing in the next 30 days)", "event": False}], "day_theme": "A work day", "current_status": "At the desk",
            "presence": {"is_afk": True, "status_label": "AWAY", "duration_minutes": 42.0, "start_time_local": "07:18 AM",
                         "most_recent_afk": None},
            "location": "Currently: Home", "weather": {"city": "Springfield", "temperature": 61, "condition": "Clear"},
            "health": "Slept poorly.",
            "chat": [{"id": "m1", "at": NOW.isoformat(), "speaker": "Owner", "text": chat}],
            "todos": [{"title": "Weekly report", "status": "needsAction", "due": ""}],
            "work": {"in_motion": [], "ended": []},
            "reminders": {"status": "available", "since": NOW.isoformat(), "through": NOW.isoformat(), "records": []},
            "tickets": [], "concerns": [{"concern_id": "c-ac", "title": "The house is at 70 for the night",
                                         "severity": "low", "changed_at": (NOW - timedelta(hours=1)).isoformat()},
                                        {"concern_id": "c-sleep", "title": "Sleep is poor", "severity": "medium",
                                         "changed_at": (NOW - timedelta(days=1)).isoformat()}], **changes}


def _gather(conn, chat="I'll start the report at 5", s=None, basis="record-1", ended=()):
    """day_tasks.gather's shape, on a scratch list: its own entries, the situation, and the call's labels
    (day_tasks.project). `basis` stands for the concerns' records and `ended` for the work relied on that has ended
    (what wakes the decider)."""
    def gather(now):
        own = [{**e, "work": None, "relies_on": [list(r) for r in L.relies_on(e["id"], connect=conn)]}
               for e in L.entries(day=DAY, kind="day", connect=conn)]
        sit = s or situation(chat)
        for c in sit["concerns"]:
            c.setdefault("basis", basis)
        return {"day": DAY, "now": now.isoformat(), "own": own, "situation": sit, "matters": [], "beliefs": [],
                "ended_relied_on": list(ended), **D.project(DAY, sit, [], own, connect=conn), "correction": None}
    return gather


def _entry(entry="new", action="new", what="The house is at 70 for the night", part="set the AC to 70",
           when="9:00 PM", why="The owner wants it at 70 at night.", concern="C1", relies_on=("C1",)):
    return {"entry": entry, "action": action, "what": what, "assistant_part": part, "when": when, "why": why,
            "concern": concern, "relies_on": list(relies_on)}


GOOD = {"entries": [_entry()]}


def test_a_calendar_concerns_entry_is_decided_and_a_run_repeats_only_when_a_stage_before_it_changed(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    calls = []
    call = lambda answer: (lambda payload: calls.append(payload) or answer)
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=call(GOOD))
    assert out == {"day_tasks": "decided", "day_list_changes": {"created": 1, "modified": 0, "unchanged": 0,
                                                                 "cancelled": 0}}
    [ac] = L.entries(concern_id="c-ac", connect=conn)
    assert ac["id"].startswith("con:") and ac["kind"] == "concern" and ac["when_local"] == "9:00 PM"
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=call(GOOD)) == \
        {"day_tasks": "unchanged"}, "nothing read has changed: no call"
    later = NOW + timedelta(minutes=10)
    assert D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, "dinner is at 6 as usual"),
                           call=call(GOOD)) == {"day_tasks": "unchanged"}, "chat alone does not wake it"
    out = D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, basis="record-2"),
                          call=call({"entries": []}))
    assert out["day_list_changes"] == {}, "an entry left out stays as it is"
    assert calls[-1]["situation"]["concerns"][0]["ref"] == "C1"
    with conn(False) as c:
        recorded = json.loads(c.execute("SELECT decisions FROM brain_day_decisions ORDER BY id").fetchone()[0])
    assert recorded == GOOD["entries"], "the run records its answer"


def test_the_end_of_work_a_decision_relied_on_wakes_it_and_a_cancellation_keeps_what_it_relied_on(tmp_path,
                                                                                                 monkeypatch):
    """2026-10-10: at 8:02 the decider cancelled the evening AC's entry as covered by the work already scheduled;
    that work was ended, and nothing woke the decider to give the concern its entry again."""
    conn = sqlite_file(tmp_path / "d.db")
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    [ac] = L.entries(concern_id="c-ac", connect=conn)
    L.apply_entries(DAY, [{"action": "cancel", "entry": ac["id"], "why": "covered by the work already scheduled",
                           "relies_on": [("work", "work_old")]}], cause="test", at=NOW, connect=conn)
    assert L.relies_on(ac["id"], connect=conn) == [("work", "work_old")], "a cancellation keeps what it relied on"
    from app.assistant.subconscious import work_links
    monkeypatch.setattr(work_links, "_rows", lambda: [{"id": "work_old", "status": "active"}])
    assert D.ended_relied_on([], [{"concern_id": "c-ac"}], connect=conn) == []
    monkeypatch.setattr(work_links, "_rows", lambda: [{"id": "work_old", "status": "abandoned"}])
    assert D.ended_relied_on([], [{"concern_id": "c-ac"}], connect=conn) == ["work_old"]
    later = NOW + timedelta(minutes=10)
    calls = []
    out = D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, ended=["work_old"]),
                          call=lambda p: calls.append(p) or {"entries": []})
    assert out["day_tasks"] == "decided" and calls, "the ending woke it"
    assert D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, ended=["work_old"]),
                           call=lambda p: pytest.fail("woken once per ending")) == {"day_tasks": "unchanged"}


def test_an_answer_without_an_entries_list_is_refused(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    seen = []
    with pytest.raises(ValueError, match="no entries list"):
        D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn),
                        call=lambda p: seen.append(p.get("correction")) or {"decisions": []})
    assert seen == [None, "no entries list"]


def test_nothing_to_decide_costs_nothing(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    never = lambda p: pytest.fail("no agent call with nothing to decide")
    empty = lambda now: {**_gather(conn, s=situation(concerns=[]))(now), "own": []}
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=empty, call=never) == \
        {"day_tasks": "nothing to decide"}


def test_the_prompts_carry_the_rules_and_the_labelled_situation():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.utils.time_utils import local_time_text
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
           "resource_assistant_capabilities": "Device control: the thermostat.",
           "resource_owner_standing_directives": "Wellness nudges are separate cron reminders."}
    system = _ENV.get_template("subconscious/day_tasks/prompts/system.j2").render(**res)
    assert "A calendar concern has no planner proposal: decide its entry yourself." in system
    assert "stays as it is when the day still needs it as it is" in system
    assert "A HEADS-UP is the opposite and belongs exactly on the boundary" in system      # when_to_interrupt.j2
    assert "You are helping the user's day go well" in system                              # stewardship.j2
    assert "Wellness nudges are separate cron reminders." in system and "Device control: the thermostat." in system
    assert "who acts" not in system.lower() and "Not yet on the list" not in system
    s = situation()
    projected = D.project(DAY, s, [], [])
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={"day": DAY, "own": [], "situation": s, "matters": [], "beliefs": [], "correction": None,
                     **projected})
    assert f"[M1] [{local_time_text(NOW.isoformat())}] Owner: I'll start the report at 5" in user
    assert "- [C1] The house is at 70 for the night (severity: low); record last changed" in user
    assert "The owner is away from the keyboard, since 07:18 AM (42 minutes)." in user
    assert "Today's calendar" not in user and "[I1]" not in user
    assert NOW.isoformat() not in user, "every time in the prompt is local"


def test_the_brain_s_view_concerns_in_full_with_their_entries_and_today_s_decisions(tmp_path, monkeypatch):
    """day_tasks reads what the brain knows: each open concern's whole record and its entries on the list, and what
    the brain read and decided today; a concern's entry and a brain decision are facts a change can cite."""
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.subconscious import concern_planner, concern_store
    conn = sqlite_file(tmp_path / "d.db")
    L.apply_entries(DAY, [{"entry": "new", "action": "new", "what": "the owner knows the family eats out tonight",
                           "assistant_part": "tell the owner the table is at 6", "when": "5:30 PM", "why": "He asked.",
                           "concern_id": "c-dinner", "relies_on": []},
                          {"entry": "new", "action": "new", "what": "the owner books the table",
                           "assistant_part": "remind the owner", "when": "4:00 PM", "why": "He asked.",
                           "concern_id": "c-dinner", "relies_on": []}],
                    cause="decided", at=NOW + timedelta(minutes=3), connect=conn)
    told, booked = L.entries(concern_id="c-dinner", connect=conn)
    L.apply_entries(DAY, [{"entry": booked["id"], "action": "cancel", "why": "He booked it himself."}],
                    cause="decided", at=NOW + timedelta(minutes=4), connect=conn)
    plan = {"decision": "act_now", "why": "The table is at 6.", "hold_until": None, "written_at": NOW.isoformat(),
            "todo": [], "proposals": [
                {"entry": told["id"], "action": "modify", "what": "the owner knows the table is at 6:30",
                 "assistant_part": "tell the owner", "when": "5:30 PM", "why": "The restaurant moved it."},
                {"entry": "new", "action": "new", "what": "the owner knows to bring the voucher",
                 "assistant_part": "remind the owner", "when": "5:45 PM", "why": "It expires today."}]}
    monkeypatch.setattr(concern_store, "load_register",
                        lambda: {"active": [{"concern_id": "c-dinner", "plan": plan}], "addressing": []})
    monkeypatch.setattr(concern_planner, "concern_text", lambda c, b: f"THE WHOLE RECORD of {c['concern_id']} ({b})")
    s = situation(concerns=[{"concern_id": "c-dinner", "bucket": "active", "title": "Dinner out tonight",
                             "severity": "low", "changed_at": NOW.isoformat()}])
    D.with_concerns(s, connect=conn)
    with conn(True) as c:
        D.ensure_schema(conn)
        from app.assistant.subconscious import brain_inbox, brain_step
        brain_inbox.ensure_schema(conn)
        brain_step.ensure_schema(conn)
        c.execute("INSERT INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, received_at) "
                  "VALUES ('chat', 'message:9', ?, 'master_room', 'Owner', 'we are eating out tonight', ?)",
                  (NOW.isoformat(), NOW.isoformat()))
        c.execute("INSERT INTO brain_matters (created_at, event_ids, concern_ids, status, decisions) VALUES (?,?,?,?,?)",
                  ((NOW + timedelta(minutes=2)).isoformat(), "[1]", "[]", "applied",
                   '{"events": {"message:9": {"decision": "new_concern", "concern_id": "c-dinner", '
                   '"reason": "Dinner plans changed."}}, "concern_updates": [], '
                   '"new_concerns": [{"title": "Dinner out tonight"}]}'))
    matters = D.matters_today(DAY, NOW + timedelta(minutes=5), connect=conn)
    projected = D.project(DAY, s, matters, [], connect=conn)
    entry = told
    assert projected["labels"]["E1"] == ["list_entry", entry["id"]] and projected["labels"]["B1"] == ["brain_matter", "1"]
    assert projected["labels"]["C1"] == ["concern", "c-dinner"]
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={"day": DAY, "own": [], "situation": s, "matters": matters, "correction": None,
                     "beliefs": [{"belief_key": "B7", "statement": "The family eats out on Fridays.", "score": 0.4}],
                     **projected})
    assert "### [C1] Dinner out tonight" in user and "THE WHOLE RECORD of c-dinner (active)" in user
    assert "- [E1] the owner knows the family eats out tonight (the assistant: tell the owner the table is at 6), 5:30 PM; open" in user
    assert "closest in meaning to the open concerns\n- The family eats out on Fridays." in user
    assert "- the owner books the table: cancelled" in user and ": He booked it himself." in user
    assert ": act now. Why: The table is at 6." in user
    assert "- change [E1] to: the owner knows the table is at 6:30 (the assistant: tell the owner), 5:30 PM. Why: The restaurant moved it." in user
    assert "- add: the owner knows to bring the voucher (the assistant: remind the owner), 5:45 PM. Why: It expires today." in user
    assert told["id"] not in user, "no id is shown"
    given = D.changeable([], s["concerns"])
    assert given == {"E1": "C1"}, "a concern's entry, changeable by the decider, belongs to its concern"
    take = {"entries": [{"entry": "E1", "action": "modify", "what": "the owner knows the table is at 6:30",
                         "assistant_part": "tell the owner", "when": "5:30 PM", "why": "The restaurant moved it.",
                         "concern": "", "relies_on": ["C1"]}]}
    assert D.entries_problems(take, given, projected["labels"]) == []
    moved = {"entries": [take["entries"][0] | {"concern": "C2"}]}
    assert any("stays with it" in p for p in D.entries_problems(moved, given, projected["labels"] | {
        "C2": ["concern", "c-other"]}))
    assert "- chat at" in user and ": we are eating out tonight" in user
    assert "the brain: new concern: Dinner plans changed." in user and "- raised a concern: Dinner out tonight" in user


def test_a_run_whose_entries_fail_is_not_recorded_and_decides_again(tmp_path, monkeypatch):
    """Until 2026-10-05 the run (its fingerprint) was recorded first: a failure applying the entries left the next
    pass "unchanged", the decisions lost."""
    conn = sqlite_file(tmp_path / "d.db")
    real = L.apply_entries

    def broken(*a, **k):
        raise OSError("list unavailable")
    monkeypatch.setattr(L, "apply_entries", broken)
    with pytest.raises(OSError):
        D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    monkeypatch.setattr(L, "apply_entries", real)
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    assert out["day_tasks"] == "decided" and out["day_list_changes"]["created"] == 1


def test_a_calendar_entry_from_before_the_cutover_is_briefed_from_the_run_that_decided_it(tmp_path):
    """A calendar entry made before 2026-10-10 (`cal:<day>:<belief>`) reads its placement from the run that decided it
    (brain_day_decisions): the day pass re-places the day, and a belief can drop out of the plan and return."""
    conn = sqlite_file(tmp_path / "d.db")
    D.ensure_schema(conn)
    item = {"label": "I3", "belief_key": "B3", "times": ["9:00 PM"], "statement": "The owner wants the AC at 70 at 9 PM.",
            "related": ["The owner likes it quiet."]}
    with conn(True) as c:
        c.execute("INSERT INTO brain_day_decisions (day, decided_at_utc, fingerprint, items, decisions) VALUES (?,?,?,?,?)",
                  (DAY, NOW.isoformat(), "f1", json.dumps([item]), json.dumps([{"item": "I3", "decision": "needed"}])))
        c.execute("INSERT INTO brain_day_decisions (day, decided_at_utc, fingerprint, items, decisions) VALUES (?,?,?,?,?)",
                  (DAY, NOW.isoformat(), "f2", "[]", "[]"))           # a later run that decided nothing
    placed = D.decided_placement(DAY, "B3", connect=conn)
    assert placed["statement"] == "The owner wants the AC at 70 at 9 PM." and placed["times"] == ["9:00 PM"]
    with pytest.raises(KeyError):
        D.decided_placement(DAY, "B404", connect=conn)


def test_only_the_stages_before_it_wake_the_decider(tmp_path):
    """2026-10-09 (owner: each stage is woken only by the stage before it): the chat, the health prose, the work in
    motion and the rest of the situation are read, never a reason to run. A concern's record or its plan does wake
    it, a calendar concern's included."""
    conn = sqlite_file(tmp_path / "d.db")
    fp = lambda **kw: D.fingerprint(_gather(conn, **kw)(NOW))
    base = fp()
    assert fp(s=situation(chat="I have a headache", health="Energy is low.",
                          work={"in_motion": [{"work_id": "w1", "list_entry": None}], "ended": []})) == base
    assert fp(basis="record-2") != base, "a concern's record changed"
    planned = situation()
    planned["concerns"][1]["plan"] = {"written_at": NOW.isoformat(), "proposals": []}
    assert fp(s=planned) != base, "its planner proposed"


# ── the decider's own entries (2026-10-06) ──────────────────────────────────

def _own(entry="new", action="new", what="Ask whether the owner is clocked in", part="ask the owner", when="now",
         why="He is back at the desk in work hours, as his directives ask.", relies_on=("D",)):
    return {"entry": entry, "action": action, "what": what, "assistant_part": part, "when": when, "why": why,
            "relies_on": list(relies_on)}


def test_an_entry_names_what_prompted_it_by_label():
    """An own entry lists in relies_on the labels of what prompted it; a modify or cancel names the entry by its label
    (O1), never its id."""
    labels = {"K1": ["calendar_event", "k"], "D": ["resource", "r"], "M1": ["message", "m1"],
              "O1": ["list_entry", "day:2026-03-10:ab"]}
    assert D.entries_problems({"entries": [_own()]}, {}, labels) == []
    assert "relies_on names nothing" in D.entries_problems({"entries": [_own(relies_on=())]}, {}, labels)[0]
    assert "not labels given" in D.entries_problems({"entries": [_own(relies_on=("X9",))]}, {}, labels)[0]
    made = {"id": "day:2026-03-10:ab", "ref": "O1", "updated_at": (NOW - timedelta(minutes=30)).isoformat()}
    given = D.changeable([made], [])
    assert D.entries_problems({"entries": [_own(entry="O1", action="modify", relies_on=("K1", "D"))]},
                              given, labels) == []
    assert D.entries_problems({"entries": [_own(entry="O1", action="cancel", what="", part="", when="",
                                                relies_on=("M1",))]}, given, labels) == []
    assert "not one of the entries given" in D.entries_problems({"entries": [_own(entry=made["id"],
                                                                                  action="modify")]}, given, labels)[0]
    assert D.entries_problems({"entries": [_own(why="M1 says he is back.")]}, {}, labels) == [], \
        "prose is never checked for labels (owner, 2026-10-09)"


def test_a_run_puts_an_own_entry_on_the_list_and_the_lane_makes_work_from_it(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "d.db")
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: {"entries": [_own()]})
    assert out["day_list_changes"]["created"] == 1
    [own] = L.entries(day=DAY, kind="day", connect=conn)
    assert own["id"].startswith(f"day:{DAY}:") and own["assistant_part"] == "ask the owner" and own["kind"] == "day"
    from app.assistant.dayflow_orchestrator import brain_list_intake as B
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    monkeypatch.setattr(B, "CALENDAR_FROM_DAY", DAY)
    out = B.deliver(get_dayflow_work_store(), now_utc=NOW, connect=sqlite_file(tmp_path / "dl.db"), list_connect=conn)
    [made] = [get_dayflow_work_store().load(w) for w in out["created"]]
    assert made.constraints["list_entry_id"] == own["id"]
    assert made.constraints["belief_refs"] == [] and made.constraints["concern_refs"] == []
    assert "the assistant's part: ask the owner" in made.constraints["rationale"]


def test_work_is_marked_with_the_entry_it_was_made_from_by_label():
    """The decider cited a work as covering the entry it was made from (2026-10-06)."""
    s = situation(work={"in_motion": [{"work_id": "w9", "list_entry": "day:2026-03-10:ab"},
                                      {"work_id": "w10", "list_entry": None},
                                      {"work_id": "w11", "list_entry": "day:2026-03-09:cd"}],
                        "ended": [{"work_id": "w7", "list_entry": "con:42"}]})
    D.project(DAY, s, [], [{"id": "day:2026-03-10:ab"}])
    assert [w["from_list"] for w in s["work"]["in_motion"]] == [
        "your own entry O1", None, "an entry on the list of 2026-03-09"]
    assert s["work"]["ended"][0]["from_list"] == "a concern's entry not shown here", "never an id"


def test_what_an_entry_relies_on_is_kept_by_id_and_shown_again_by_label(tmp_path, monkeypatch):
    """2026-10-08: the decider's reason "W21 abandoned the reminder ..., but W29 records that reminder for Wednesday's
    walk" was stored with its labels; the next run read W21/W29 that named nothing. relies_on is mapped to ids and
    kept as relations; the next run shows those objects under its own labels, an object no longer in view among the
    earlier things."""
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.subconscious import relations
    conn = sqlite_file(tmp_path / "d.db")
    s1 = situation(chat="the AC can wait until 9", work={"in_motion": [], "ended": [
        {"work_id": "work_yesterday", "title": "Set the AC yesterday", "status": "done", "objective": "",
         "created": "Mon 2026-03-09", "updated": "Mon 2026-03-09", "ended_because": "done", "outcomes": [],
         "link": "ended", "score": None, "list_entry": None}]})
    answer = {"entries": [_own(what="The AC is at 70 at 9", part="set the AC to 70", when="9:00 PM",
                               why="Yesterday's setting was for yesterday; the owner asked again today.",
                               relies_on=("W1", "M1"))]}
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn, s=s1), call=lambda p: answer)
    [own] = L.entries(day=DAY, kind="day", connect=conn)
    assert relations.targets(("list_entry", own["id"]), "relies_on", connect=conn) == [("work", "work_yesterday"),
                                                                                       ("message", "m1")]
    monkeypatch.setattr(D, "describe", lambda kind, obj_id, connect=None: {
        "work": {**s1["work"]["ended"][0]}} if kind == "work" else {"missing": True})
    seen = []
    s2 = situation(chat="something else")
    s2["chat"][0]["id"] = "m2"
    D.run_day_tasks(now_utc=NOW + timedelta(minutes=10), connect=conn,
                    gather_inputs=_gather(conn, s=s2, basis="record-2"), call=lambda p: seen.append(p) or {"entries": []})
    [mine] = seen[0]["own"]
    assert mine["relies_on_labels"] == ["W1", "M2"], "relabelled in the new call"
    assert seen[0]["labels"]["W1"] == ["work", "work_yesterday"] and seen[0]["labels"]["M2"] == ["message", "m1"]
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(agent_input=seen[0])
    assert "relies on: [W1], [M2]" in user and "## Earlier things your entries rely on" in user
    assert "### Set the AC yesterday [W1; done;" in user and "[M2] (no longer stored)" in user
    assert "work_yesterday" not in user and own["id"] not in user, "no id is shown"


def test_an_entry_for_a_concern_is_the_concerns(tmp_path, monkeypatch):
    """2026-10-08: an own entry made for a concern carried no link to it. Since 2026-10-09 a new entry that serves a
    concern is the concern's own entry (con:, kind concern): intake attaches its work to the concern, and it is
    cancelled when the concern closes."""
    conn = sqlite_file(tmp_path / "d.db")
    answer = {"entries": [_own(what="Ask whether he slept", part="ask the owner",
                               why="His sleep concern is open.") | {"concern": "C2"}]}
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: answer)
    [own] = L.entries(concern_id="c-sleep", connect=conn)
    assert own["id"].startswith("con:") and own["kind"] == "concern" and own["day"] is None
    assert L.entries(day=DAY, kind="day", connect=conn) == []
    from app.assistant.dayflow_orchestrator import brain_list_intake as B
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from app.assistant.subconscious import concern_store
    monkeypatch.setattr(concern_store, "load_register", lambda connect=None: {
        "active": [{"concern_id": "c-sleep"}], "addressing": [], "resolved": [], "dormant": []})
    monkeypatch.setattr(B, "CALENDAR_FROM_DAY", DAY)
    monkeypatch.setattr(B, "CONCERN_SINCE", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(B, "brief", lambda entry: f"BRIEF of {entry['id']}")
    out = B.deliver(get_dayflow_work_store(), now_utc=NOW, connect=sqlite_file(tmp_path / "dl.db"), list_connect=conn)
    [work] = [get_dayflow_work_store().load(w) for w in out["created"]]
    assert work.constraints["concern_refs"] == ["concern:c-sleep"], "its work is attached to the concern"
    closed = L.cancel_for_closed_concerns({"active": [], "addressing": [],
                                           "resolved": [{"concern_id": "c-sleep", "resolution_reason": "sleeps well"}]},
                                          at=NOW, connect=conn)
    assert closed == [own["id"]] and L.entries(concern_id="c-sleep", connect=conn) == []
    labels = {"C1": ["concern", "c-sleep"], "W1": ["work", "w"], "D": ["resource", "r"]}
    assert "concern 'W1' is not an open concern given" in D.entries_problems(
        {"entries": [_own() | {"concern": "W1"}]}, {}, labels)[0]


def test_earlier_entries_whose_work_is_in_motion_are_the_deciders_to_rewrite(tmp_path):
    """2026-10-09: to move a check-in that was a step of work made from Thursday's entry, the day's tasks (seeing only
    Friday's entries) re-decided Friday's entry, and intake made new work asking a worker to move the check-in.
    Earlier days' open entries are listed with the decider's own; to change live work it rewrites its entry."""
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    conn = sqlite_file(tmp_path / "d.db")
    made = lambda what, when: {"entry": "new", "action": "new", "what": what, "assistant_part": what, "when": when,
                               "why": "w", "relies_on": []}
    L.apply_entries("2026-03-09", [made("hourly check-ins", "10:00 AM"), made("walk the dogs", "8:00 AM")],
                    cause="test", at=NOW, connect=conn)
    checkins, dogs = L.entries(day="2026-03-09", kind="day", connect=conn)
    L.apply_entries("2026-03-09", [{"entry": dogs["id"], "action": "cancel", "why": "done"}], cause="test", at=NOW,
                    connect=conn)
    assert [e["id"] for e in L.open_before(DAY, connect=conn)] == [checkins["id"]], "open ones only"
    carried = {**L.open_before(DAY, connect=conn)[0], "work": None, "relies_on": []}
    projected = D.project(DAY, situation(), [], [carried])
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(agent_input={
        "day": DAY, "own": [carried], "situation": situation(), "matters": [], "beliefs": [], "correction": None,
        **projected})
    assert "- [O1] (from 2026-03-09, its work still in motion) hourly check-ins" in user
    L.apply_entries(DAY, [{"entry": carried["id"], "action": "modify", "what": "hourly check-ins from 10:20",
                           "assistant_part": "hourly check-ins", "when": "10:20 AM", "why": "after the standup",
                           "relies_on": []}], cause="test", at=NOW + timedelta(minutes=5), connect=conn)
    [entry] = [e for e in L.open_before(DAY, connect=conn) if e["id"] == carried["id"]]
    assert entry["when_local"] == "10:20 AM" and entry["day"] == "2026-03-09", "the owning entry changed, so its work will"
