"""The stories behind the owner's /mind page: the brain's work told in plain words.

Pins: a matter is told once, with a line for every event, from the events, the decisions and where a new
concern landed; an answer missing a line gets one correction, and one still invalid is stored as failed
and not retried until its record changes. An open concern with a brief is told again when its plan or
work changes; nothing is proposed for a hold. The day is told from its matter stories. The page's view
of a day gives its ribbon, each event's line and tag, what was set aside without a concern, a card per open
concern, and the day's calendar. Scratch store; fake agent calls;
invented data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import brain_inbox, brain_step, brain_stories
from app.assistant.tests.concern_store_helpers import ScratchRegister

NOW = datetime.now(timezone.utc)
HOUR_AGO = (NOW - timedelta(hours=1)).isoformat()
BRIEF = {"what": "The library says a book is overdue.", "why_it_matters": "A fine grows.", "known": [],
         "tried": "Nothing yet.", "owner_wishes": "Nothing said.", "open_questions": [], "basis": "b1",
         "recommendation": "Check the account."}
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}


@pytest.fixture(autouse=True)
def conversations(monkeypatch):
    """The conversation view without the chat store: each event as its own line, marked."""
    from app.assistant.subconscious import conversations as conv
    monkeypatch.setattr(conv, "build", lambda events, now_utc=None: events)
    monkeypatch.setattr(conv, "render", lambda events: "\n".join(f"[{e['mark']}] {e['text']}" for e in events))


@pytest.fixture
def reg(tmp_path):
    return ScratchRegister(tmp_path, "brain.db")


def _event(connect, eid, source, text, *, route="new_matter", concern_ids=(), reasoning="r"):
    brain_inbox.ensure_schema(connect)
    with connect(True) as c:
        c.execute("INSERT INTO brain_events (id, source, source_ref, occurred_at, speaker, text, received_at, "
                  "gate_status, route, concern_ids, gate_reasoning) VALUES (?, ?, ?, ?, ?, ?, ?, 'routed', ?, ?, ?)",
                  (eid, source, f"{source}:{eid}", HOUR_AGO, "Sam", text, HOUR_AGO, route, json.dumps(list(concern_ids)),
                   reasoning))


def _matter(connect, event_ids, decisions, admitted=None):
    brain_step.ensure_schema(connect)
    with connect(True) as c:
        c.execute("INSERT INTO brain_matters (created_at, event_ids, concern_ids, status, decisions, admitted) "
                  "VALUES (?, ?, '[]', 'applied', ?, ?)",
                  (HOUR_AGO, json.dumps(event_ids), json.dumps(decisions), json.dumps(admitted or {})))


class Agents:
    """Fake story agents: answers per kind; records every payload."""
    def __init__(self, **answers):
        self.answers, self.seen = answers, []

    def __call__(self, kind):
        def call(payload):
            self.seen.append((kind, payload))
            answer = self.answers[kind]
            return answer(payload) if callable(answer) else answer
        return call


def _lines(payload):
    return {"headline": "A library notice", "lines": [{"event": e["label"], "line": f"told {e['label']}"}
                                                      for e in payload["events"]]}


CONCERN_STORY = {"state": "on hold", "story": "Nothing done yet.", "proposed": [], "fate": "Looked at Monday."}
DAY_STORY = {"headline": "A quiet day", "overview": "One email came in."}


def _seed_matter(reg):
    reg.write({**EMPTY, "active": [{"concern_id": "c-lib", "title": "Library fine", "kind": "anticipated_need",
                                    "brief": BRIEF, "plan": {"decision": "hold", "todo": [], "why": "w",
                                                             "hold_until": "2030-01-01 09:00", "basis": "b1"}}]})
    _event(reg.connect, 1, "email", "Your book is overdue.")
    _event(reg.connect, 2, "chat", "ok", route="none", reasoning="small talk")
    _matter(reg.connect, [1], {"events": {"email:1": {"decision": "tracked_as_new", "reason": "a fine"}},
                               "concern_updates": [],
                               "new_concerns": [{"label": "N1", "title": "Library fine", "door": "merge_open",
                                                 "door_reason": "same"}]},
            admitted={"N1": "c-lib"})


def _run(reg, agents):
    return brain_stories.run_stories(call=agents, connect=reg.connect, register_connect=reg.connect, now_utc=NOW,
                                     handed_on=lambda a, b: 1)


def test_a_matter_a_concern_and_the_day_are_told_once(reg):
    _seed_matter(reg)
    agents = Agents(matter=_lines, concern=CONCERN_STORY, day=DAY_STORY)
    assert _run(reg, agents) == {"stories_written": 3, "stories_failed": 0}
    kinds = [k for k, _ in agents.seen]
    assert kinds == ["matter", "concern", "day"], "the day is told after its matters"
    matter = agents.seen[0][1]
    assert matter["new_concerns"] == [{"title": "Library fine", "outcome": "joined the open concern “Library fine”"}]
    assert matter["events"][0]["decision"] == "tracked_as_new" and matter["events"][0]["source"] == "email"
    assert matter["conversation"] == "[E1] Your book is overdue."
    day = agents.seen[2][1]
    assert day["matters"][0]["headline"] == "A library notice" and day["handed_on"] == 1
    assert day["counts"] == {"emails": 1, "chat messages": 1}
    assert _run(reg, agents) == {"stories_written": 0, "stories_failed": 0}


def test_a_missing_line_is_corrected_once_then_stored_as_failed(reg):
    _seed_matter(reg)
    agents = Agents(matter={"headline": "h", "lines": []}, concern=CONCERN_STORY, day=DAY_STORY)
    assert _run(reg, agents)["stories_failed"] == 1
    matters = [p for k, p in agents.seen if k == "matter"]
    assert len(matters) == 2 and "no line for ['E1']" in matters[1]["correction"]
    assert "no line" in brain_stories.load("matter", reg.connect)["1"]["error"]
    agents.seen.clear()
    _run(reg, agents)
    assert [k for k, _ in agents.seen] == [], "a failed story waits for its record to change"


def test_nothing_is_proposed_for_a_hold_and_a_new_plan_retells_the_concern(reg):
    _seed_matter(reg)
    agents = Agents(matter=_lines, concern={**CONCERN_STORY, "proposed": ["Pay it"]}, day=DAY_STORY)
    _run(reg, agents)
    told = [p for k, p in agents.seen if k == "concern"]
    assert "nothing is proposed now" in told[1]["correction"]
    register = reg.read()
    register["active"][0]["plan"] = {"decision": "act_now", "todo": ["Pay the fine"], "why": "due", "basis": "b1"}
    reg.write(register)
    agents.seen.clear()
    agents.answers["concern"] = {**CONCERN_STORY, "proposed": ["Pay the fine"]}
    assert _run(reg, agents)["stories_written"] == 1
    assert agents.seen[0][1]["concern"]["plan"]["todo"] == ["Pay the fine"]


def test_the_day_view_shows_today_s_calendar(reg):
    from app.assistant.subconscious import day_plan
    day = brain_stories._window_days(NOW)[-1]
    line = {"time": "9:00 PM", "belief_key": "B1", "statement": "Takes the trash out on Wednesday nights.", "modifiers": []}
    day_plan.save_plan({"day": day, "generated_at_utc": "x", "inputs_fingerprint": "f", "selected_belief_keys": ["B1"],
                        "qualifier_belief_keys": [], "selection_reason": "", "change_summary": "", "backdrops": [],
                        "flags": [{**line, "time": "all day"}],
                        "slots": [{"slot": "s", "hour_local_iso": f"{day}T21:00:00", "items": [], "lines": [line]}]},
                       connect=reg.connect)
    view = brain_stories.day_view(day, now_utc=NOW, connect=reg.connect, register_connect=reg.connect,
                                  handed_on=lambda a, b: 0)
    assert view["calendar"] == [{"time": "all day", "statement": line["statement"]},
                                {"time": "9:00 PM", "statement": line["statement"]}]


def test_the_day_view_gives_the_ribbon_the_lines_and_the_cards(reg):
    _seed_matter(reg)
    _run(reg, Agents(matter=_lines, concern=CONCERN_STORY, day=DAY_STORY))
    # The day the events were seeded on: an hour back is yesterday between midnight and 1 AM.
    day = brain_stories._local(HOUR_AGO).date().isoformat()
    view = brain_stories.day_view(day, now_utc=NOW, connect=reg.connect, register_connect=reg.connect,
                                  handed_on=lambda a, b: 1)
    assert view["story"] == DAY_STORY and view["today"] == (day == brain_stories._window_days(NOW)[-1])
    assert view["ribbon"] == {"came_in": 2, "by_source": {"email": 1, "chat": 1}, "matters": 1,
                              "concerns_touched": 1, "handed_on": 1, "settled": 0}
    [item] = view["matters"][0]["items"]
    assert (item["line"], item["tag"]) == ("told E1", "joined a concern")
    assert view["set_aside"][0]["reason"] == "small talk"
    [card] = view["concerns"]
    assert card["story"] == CONCERN_STORY and card["decision"] == "hold" and view["calendar"] == []


def test_an_event_that_settled_a_concern_is_tagged_settled():
    e1, e2 = {"source_ref": "work:1"}, {"source_ref": "chat:2"}
    used = {"work:1": {"decision": "used"}, "chat:2": {"decision": "used"}}
    cited = {"decisions": {"events": used, "concern_updates": [
        {"concern_id": "c", "action": "resolve", "note": "n", "events": ["work:1"]},
        {"concern_id": "d", "action": "note", "note": "n", "events": ["chat:2"]}]}}
    assert brain_stories._tag(e1, cited) == ("settled", "settled")
    assert brain_stories._tag(e2, cited) == ("noted", "noted")
    older = {"decisions": {"events": used, "concern_updates": [{"concern_id": "c", "action": "resolve", "note": "n"}]}}
    assert brain_stories._tag(e1, older) == ("settled", "settled")
