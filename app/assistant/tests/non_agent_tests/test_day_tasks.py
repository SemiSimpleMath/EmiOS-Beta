"""The day's tasks: the brain decides today's calendar, in shadow (docs/design/brain_delegates_work_2026-10-04.md).

Pins: each placement is offered once (belief and time), labelled in order; one done elsewhere or more than an hour
past is not offered; every item not yet on the list is decided once, a needed one names the assistant's part and
when, and covers only name other items; an item on the list keeps its entry when the answer leaves it out, and a
change to it must cite a fact newer than the entry (refused otherwise: one correction round, then raised); a run is
recorded, and repeated only when something it read has changed; nothing is handed to dayflow; the comparison sets
the list's entries beside what dayflow did with the same beliefs; the prompts render the rules, the two groups of
items, the labelled facts and the brain's situation, times local. Scratch database; fake agent; invented data only.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_list as L
from app.assistant.subconscious import day_tasks as D
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
DAY = "2026-03-10"


def _plan():
    line = lambda key, time, text: {"belief_key": key, "time": time, "statement": text,
                                    "related": [{"belief_key": "B9", "statement": "The owner likes it quiet."}]}
    return {"flags": [line("B1", "all day", "The owner sometimes starts the weekly report on Sunday.")],
            "slots": [{"lines": [line("B2", "6:00 PM", "Family dinner is at 6 PM."),
                                 line("B3", "9:00 PM", "The owner wants the AC at 70 at 9 PM.")]},
                      {"lines": [line("B2", "6:00 PM", "Family dinner is at 6 PM.")]}]}


def situation(chat="I'll start the report at 5", **changes):
    """A situation as subconscious/situation.gather returns it; invented data."""
    return {"now": NOW.isoformat(), "day": DAY, "weekday": "Tuesday",
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
            "tickets": [], "concerns": [{"title": "Sleep is poor", "severity": "medium",
                                         "changed_at": (NOW - timedelta(days=1)).isoformat()}], **changes}


def _gather(conn, chat="I'll start the report at 5", refs=None):
    """day_tasks.gather's shape, on a scratch list: the plan's placements with their entries, offered as of NOW."""
    def gather(now):
        every = D.placements(_plan())
        return {"day": DAY, "now": now.isoformat(),
                "items": D.offered(DAY, D.with_current(DAY, every, connect=conn), now),
                "situation": situation(chat), "matters": [], "refs": refs or {"M1": NOW.isoformat()}, "own": [],
                "correction": None}
    return gather


def _d(item, decision, part="", when="", covers=(), why="w", new_since=()):
    return {"item": item, "decision": decision, "assistant_part": part, "when": when, "covers": list(covers),
            "why": why, "new_since": list(new_since)}


GOOD = {"decisions": [
    _d("I1", "not_needed", why="The owner said in chat he starts at 5."),
    _d("I2", "needed", "tell the owner dinner is starting", "6:00 PM", why="A heads-up on the boundary."),
    _d("I3", "needed", "set the AC to 70", "9:00 PM", why="A standing wish.")]}


def _new_items():
    return [{**it, "current": None} for it in D.placements(_plan())]


def test_each_placement_is_offered_once_in_the_plans_order():
    items = D.placements(_plan())
    assert [(i["label"], i["belief_key"], i["times"]) for i in items] == [
        ("I1", "B1", ["all day"]), ("I2", "B2", ["6:00 PM"]), ("I3", "B3", ["9:00 PM"])]
    twice = {"flags": [], "slots": [{"lines": [_plan()["slots"][0]["lines"][1]]},
                                    {"lines": [{**_plan()["slots"][0]["lines"][1], "time": "9:30 PM"}]}]}
    assert [(i["belief_key"], i["times"]) for i in D.placements(twice)] == [("B3", ["9:00 PM", "9:30 PM"])],         "one item per belief, with every time the plan places it"
    assert items[0]["related"] == ["The owner likes it quiet."]


def test_what_is_done_elsewhere_or_past_is_not_offered_again():
    items = _new_items()
    items[0]["current"] = {"decision": "done_elsewhere", "assistant_part": "", "when": "", "why": "chat",
                           "decided_at": NOW.isoformat()}
    late = datetime(2026, 3, 11, 3, 30, tzinfo=timezone.utc)        # 8:30 PM local: 6 PM is more than an hour past
    assert [(i["label"], i["belief_key"]) for i in D.offered(DAY, items, late)] == [("I1", "B3")]


@pytest.mark.parametrize("change,problem", [
    (lambda d: d["decisions"].pop(), "not decided: ['I3']"),
    (lambda d: d["decisions"].append(dict(d["decisions"][0])), "decided more than once: ['I1']"),
    (lambda d: d["decisions"][1].update(when=""), "I2: needed names when"),
    (lambda d: d["decisions"][1].update(covers=["I9"]), "I2: covers ['I9']"),
    (lambda d: d["decisions"][0].update(covers=["I2"]), "I1: only a needed item covers others"),
    (lambda d: d["decisions"][0].update(decision="maybe"), "I1: decision 'maybe'"),
])
def test_every_new_item_is_decided_once(change, problem):
    import copy
    data = copy.deepcopy(GOOD)
    change(data)
    assert any(problem in p for p in D.problems(data, _new_items(), {}))
    assert D.problems(GOOD, _new_items(), {}) == []


def test_an_item_on_the_list_changes_only_with_something_newer():
    items = _new_items()
    items[2]["current"] = {"decision": "needed", "assistant_part": "set the AC to 70", "when": "9:00 PM", "why": "w",
                           "decided_at": NOW.isoformat()}
    refs = {"M1": (NOW - timedelta(hours=1)).isoformat(), "M2": (NOW + timedelta(minutes=5)).isoformat()}
    keep = {"decisions": GOOD["decisions"][:2]}
    assert D.problems(keep, items, refs) == [], "left out is kept"
    moved = lambda since: {"decisions": [*GOOD["decisions"][:2],
                                          _d("I3", "needed", "set the AC to 70", "8:30 PM", new_since=since)]}
    assert any("nothing cited is newer" in p for p in D.problems(moved([]), items, refs))
    assert any("nothing cited is newer" in p for p in D.problems(moved(["M1"]), items, refs)), "older than the entry"
    assert any("not labels given" in p for p in D.problems(moved(["M9"]), items, refs))
    assert D.problems(moved(["M2"]), items, refs) == []


def test_a_run_decides_the_new_keeps_the_rest_and_changes_only_on_something_newer(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    calls = []
    call = lambda answer: (lambda payload: calls.append(payload) or answer)
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=call(GOOD))
    assert out == {"day_tasks": "decided", "day_items": 3, "day_decided": 3,
                   "day_list_changes": {"created": 3, "modified": 0, "unchanged": 0, "kept": 0}}
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=call(GOOD)) == \
        {"day_tasks": "unchanged"}, "nothing read has changed: no call"
    later = NOW + timedelta(minutes=10)
    out = D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, "dinner is at 6 as usual"),
                          call=call({"decisions": []}))
    assert out["day_list_changes"] == {"created": 0, "modified": 0, "unchanged": 0, "kept": 3}
    assert calls[-1]["items"][2]["current"]["when"] == "9:00 PM", "the decider sees each entry it would keep"
    move = {"decisions": [_d("I3", "needed", "set the AC to 70", "8:30 PM", new_since=["M1"],
                             why="The owner asked for 8:30 today.")]}
    out = D.run_day_tasks(now_utc=later + timedelta(minutes=10), connect=conn,
                          gather_inputs=_gather(conn, "AC at 8:30 today", refs={"M1": (later + timedelta(minutes=5)).isoformat()}),
                          call=call(move))
    assert out["day_list_changes"]["modified"] == 1 and out["day_list_changes"]["kept"] == 2
    [ac] = [e for e in L.entries(day=DAY, connect=conn) if e["belief_key"] == "B3"]
    assert ac["when_local"] == "8:30 PM" and ac["who_acts"] == ""


def test_a_change_with_nothing_new_is_refused_after_one_correction(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    rejudge = {"decisions": [_d("I2", "not_needed", why="On reflection the heads-up is not needed.")]}
    seen = []
    with pytest.raises(ValueError, match="still invalid"):
        D.run_day_tasks(now_utc=NOW + timedelta(minutes=10), connect=conn,
                        gather_inputs=_gather(conn, "something else"),
                        call=lambda p: seen.append(p.get("correction")) or rejudge)
    assert seen[0] is None and "I2 is on the list since" in seen[1] and "leave it out to keep it" in seen[1]
    [dinner] = [e for e in L.entries(day=DAY, connect=conn) if e["belief_key"] == "B2"]
    assert dinner["decision"] == "needed", "the refused change wrote nothing"


def test_no_plan_or_no_placements_costs_nothing(tmp_path):
    conn = sqlite_file(tmp_path / "d.db")
    never = lambda p: pytest.fail("no agent call without placements")
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: None, call=never) == \
        {"day_tasks": "no plan"}
    assert D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=lambda now: {**_gather(conn)(now), "items": []},
                           call=never) == {"day_tasks": "no placements"}


def test_the_comparison_sets_the_list_beside_dayflow(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "d.db")
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    monkeypatch.setattr(D, "dayflow_handling", lambda day: {"B1": ["all day: converted_to_work_object:work_x"]})
    rows = {r["belief_key"]: r for r in D.compare(DAY, connect=conn)}
    assert rows["B1"]["decision"] == "not_needed" and rows["B1"]["dayflow"] == ["all day: converted_to_work_object:work_x"]
    assert rows["B3"]["dayflow"] == []


def test_the_prompts_carry_the_rules_the_two_groups_and_the_labelled_situation():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.utils.time_utils import local_time_text
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
           "resource_assistant_capabilities": "Device control: the thermostat.",
           "resource_owner_standing_directives": "Wellness nudges are separate cron reminders."}
    system = _ENV.get_template("subconscious/day_tasks/prompts/system.j2").render(**res)
    assert "stays as it is when you leave it out" in system and "Name those labels in new_since" in system
    assert "A HEADS-UP is the opposite and belongs exactly on the boundary" in system      # when_to_interrupt.j2
    assert "You are helping the user's day go well" in system                              # stewardship.j2
    assert "Wellness nudges are separate cron reminders." in system and "Device control: the thermostat." in system
    assert "away from the keyboard" in system and "who acts" not in system.lower()
    items = _new_items()
    items[2]["current"] = {"decision": "needed", "assistant_part": "set the AC to 70", "when": "9:00 PM", "why": "w",
                           "decided_at": NOW.isoformat()}
    s = situation()
    s["chat"][0]["ref"] = "M1"
    s["concerns"][0]["ref"] = "C1"
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={"day": DAY, "items": items, "own": [], "situation": s, "matters": [], "correction": None})
    new_part, listed_part = user.split("### Already on the list", 1)
    assert "[I1] All day: The owner sometimes starts the weekly report on Sunday." in new_part
    assert "    read with: The owner likes it quiet." in new_part
    assert "[I3] At 9:00 PM" in listed_part
    assert f"on the list since {local_time_text(NOW.isoformat())}: needed: set the AC to 70, 9:00 PM" in listed_part
    assert f"[M1] [{local_time_text(NOW.isoformat())}] Owner: I'll start the report at 5" in user
    assert "- [C1] Sleep is poor (severity: medium); record last changed" in user
    assert "The owner is away from the keyboard, since 07:18 AM (42 minutes)." in user
    assert NOW.isoformat() not in user, "every time in the prompt is local"


def test_the_brain_s_view_concerns_in_full_with_their_entries_and_today_s_decisions(tmp_path, monkeypatch):
    """day_tasks reads what the brain knows: each open concern's whole record and its entries on the list, and what
    the brain read and decided today; a concern's entry and a brain decision are facts a change can cite."""
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.subconscious import concern_planner, concern_store
    conn = sqlite_file(tmp_path / "d.db")
    L.apply_concern("c-dinner", [{"action": "new", "what": "the owner knows the family eats out tonight",
                                  "who_acts": "", "assistant_part": "tell the owner the table is at 6", "when": "5:30 PM",
                                  "why": "He asked."}], cause="plan", at=NOW + timedelta(minutes=3), connect=conn)
    monkeypatch.setattr(concern_store, "load_register", lambda: {"active": [{"concern_id": "c-dinner"}], "addressing": []})
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
                   '{"events": {"message:9": {"decision": "tracked_as_new", "reason": "Dinner plans changed."}}, '
                   '"concern_updates": [], "new_concerns": [{"title": "Dinner out tonight"}]}'))
    matters = D.matters_today(DAY, NOW + timedelta(minutes=5), connect=conn)
    refs = D.label_refs(s, matters)
    assert refs["E1"] == (NOW + timedelta(minutes=3)).isoformat() and refs["B1"] == (NOW + timedelta(minutes=2)).isoformat()
    items = _new_items()
    items[1]["current"] = {"decision": "needed", "assistant_part": "tell the owner dinner is starting", "when": "6:00 PM",
                           "why": "w", "decided_at": NOW.isoformat()}
    drop = {"decisions": [GOOD["decisions"][0], GOOD["decisions"][2],
                          _d("I2", "not_needed", why="E1 covers it: the family eats out.", new_since=["E1"])]}
    assert D.problems(drop, items, refs) == [], "a concern's newer entry licenses the change"
    user = _ENV.get_template("subconscious/day_tasks/prompts/user.j2").render(
        agent_input={"day": DAY, "items": items, "own": [], "situation": s, "matters": matters, "correction": None})
    assert "### [C1] Dinner out tonight" in user and "THE WHOLE RECORD of c-dinner (active)" in user
    assert "- [E1] the owner knows the family eats out tonight (the assistant: tell the owner the table is at 6), 5:30 PM; open" in user
    assert "- chat at" in user and ": we are eating out tonight" in user
    assert "the brain: tracked as new: Dinner plans changed." in user and "- raised a concern: Dinner out tonight" in user


def test_a_run_whose_entries_fail_is_not_recorded_and_decides_again(tmp_path, monkeypatch):
    """Until 2026-10-05 the run (its fingerprint) was recorded first: a failure applying the entries left the next
    pass "unchanged", the decisions lost."""
    conn = sqlite_file(tmp_path / "d.db")
    real = L.apply_calendar

    def broken(*a, **k):
        raise OSError("list unavailable")
    monkeypatch.setattr(L, "apply_calendar", broken)
    with pytest.raises(OSError):
        D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    monkeypatch.setattr(L, "apply_calendar", real)
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    assert out["day_tasks"] == "decided" and out["day_list_changes"]["created"] == 3


def test_an_entry_is_briefed_from_the_run_that_decided_it_not_the_current_plan(tmp_path):
    """The day pass re-places the day; a belief can drop out of the plan and return. The brief read the current plan
    and raised, stopping every planning tick until the belief returned (found 2026-10-05)."""
    conn = sqlite_file(tmp_path / "d.db")
    D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn), call=lambda p: GOOD)
    later = NOW + timedelta(minutes=10)
    D.run_day_tasks(now_utc=later, connect=conn, gather_inputs=_gather(conn, "dinner is at 6 as usual"),
                    call=lambda p: {"decisions": []})                         # a later run that decided nothing
    placed = D.decided_placement(DAY, "B3", connect=conn)
    assert placed["statement"] == "The owner wants the AC at 70 at 9 PM." and placed["times"] == ["9:00 PM"]
    assert placed["related"] == ["The owner likes it quiet."]
    with pytest.raises(KeyError):
        D.decided_placement(DAY, "B404", connect=conn)


def test_the_health_assessment_alone_is_not_new_input():
    """2026-10-05: the health prose is reworded on every inference run, and the day's tasks called the model about
    every ten minutes to decide nothing. Owner: health out of the fingerprint; the decider reads it when it runs."""
    from app.assistant.subconscious.situation import fingerprint_view
    a = situation(health="Slept well; energy normal.")
    b = situation(health="Energy is low; a headache since noon.")
    assert fingerprint_view(a) == fingerprint_view(b)
    assert fingerprint_view(situation(chat="I have a headache")) != fingerprint_view(a)


# ── the decider's own entries (2026-10-06) ──────────────────────────────────

def _own(entry="new", action="new", what="Ask whether the owner is clocked in", part="ask the owner", when="now",
         why="He is back at the desk in work hours (directive D).", cites=("D",)):
    return {"entry": entry, "action": action, "what": what, "assistant_part": part, "when": when, "why": why,
            "cites": list(cites)}


def test_an_own_entry_cites_what_prompted_it_and_changes_only_on_something_newer():
    """On 2026-10-06 the decider, with nowhere to put an act no belief asked for, wrote a clock-in check into the
    encouragement belief's entry and a dinner question into family time's. An own entry carries such an act; it cites
    a label given; the schedule (K) and the standing directives (D) date from the day's start, so they justify a new
    entry but never a change to one made today."""
    refs = {"K1": "2026-03-10T12:00:00+00:00", "D": "2026-03-10T12:00:00+00:00", "M1": NOW.isoformat()}
    assert D.own_problems({"own_entries": [_own()]}, [], refs) == []
    assert "cites names nothing" in D.own_problems({"own_entries": [_own(cites=())]}, [], refs)[0]
    assert "not labels given" in D.own_problems({"own_entries": [_own(cites=("X9",))]}, [], refs)[0]
    made = {"id": "day:2026-03-10:ab", "updated_at": (NOW - timedelta(minutes=30)).isoformat()}
    stale = D.own_problems({"own_entries": [_own(entry=made["id"], action="modify", cites=("K1", "D"))]}, [made], refs)
    assert "nothing cited is newer" in stale[0]
    assert D.own_problems({"own_entries": [_own(entry=made["id"], action="cancel", what="", part="", when="",
                                                cites=("M1",))]}, [made], refs) == []
    assert "not one of your own entries" in D.own_problems({"own_entries": [_own(entry="day:x", action="modify")]},
                                                           [made], refs)[0]


def test_a_run_puts_an_own_entry_on_the_list_and_the_calendar_lane_makes_work_from_it(tmp_path, monkeypatch):
    conn = sqlite_file(tmp_path / "d.db")
    answer = {**GOOD, "own_entries": [_own()]}
    out = D.run_day_tasks(now_utc=NOW, connect=conn, gather_inputs=_gather(conn, refs={"M1": NOW.isoformat(),
                                                                                      "D": NOW.isoformat()}),
                          call=lambda p: answer)
    assert out["day_list_changes"]["own"]["created"] == 1
    [own] = L.entries(day=DAY, kind="day", connect=conn)
    assert own["id"].startswith(f"day:{DAY}:") and own["assistant_part"] == "ask the owner" and own["kind"] == "day"
    assert len(L.entries(day=DAY, connect=conn)) == 3, "the placements' entries are apart from the own entries"
    from app.assistant.dayflow_orchestrator import brain_list_intake as B
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    live = {"calendar": {"mode": "live", "from_day": DAY}, "concern": {"mode": "off", "since": None},
            "steward": {"mode": "retired"}}
    real_brief = B.brief                          # an own entry's brief renders from the entry alone
    monkeypatch.setattr(B, "brief", lambda entry: real_brief(entry) if entry["kind"] == "day" else f"BRIEF of {entry['id']}")
    out = B.deliver(get_dayflow_work_store(), now_utc=NOW, connect=sqlite_file(tmp_path / "dl.db"), list_connect=conn,
                    lane_config=live)
    made = [get_dayflow_work_store().load(w) for w in out["created"]]
    mine = [w for w in made if w.constraints["list_entry_id"] == own["id"]]
    assert len(mine) == 1 and mine[0].constraints["belief_refs"] == [] and mine[0].constraints["concern_refs"] == []
    assert "the assistant's part: ask the owner" in mine[0].constraints["rationale"]


def test_work_is_marked_with_the_entry_it_was_made_from():
    """The decider cited W8, the family-time entry's own work, as covering that entry, and decided it not needed."""
    s = situation(work={"in_motion": [{"work_id": "w8", "list_entry": f"cal:{DAY}:B2"},
                                      {"work_id": "w9", "list_entry": "day:2026-03-10:ab"},
                                      {"work_id": "w10", "list_entry": None}],
                        "ended": [{"work_id": "w7", "list_entry": "con:42"}]})
    items = [{"label": "I2", "belief_key": "B2"}]
    D.mark_list_work(s, DAY, items, [{"id": "day:2026-03-10:ab"}])
    assert [w["from_list"] for w in s["work"]["in_motion"]] == ["your entry for I2", "your own entry day:2026-03-10:ab",
                                                                None]
    assert s["work"]["ended"][0]["from_list"] == "list entry con:42"
