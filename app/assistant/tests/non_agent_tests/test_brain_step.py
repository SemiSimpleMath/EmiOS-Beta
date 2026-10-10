"""The brain step: what the gate passed on, read one matter at a time, applied to the concerns.

Pins: events group into matters (by the concern they bear on, else by conversation or thread);
a note lands on the concern as a [brain] journal line with the events as evidence; a resolution
closes it; a new concern's source is the event it comes from, copied by code, and it carries when it expires;
every event is decided once, and an event changes one concern (docs/design/concerns_as_state_2026-10-09.md, rule 3);
an invalid answer gets one correction, then the matter is recorded failed, nothing is written and it is not retried;
the brain reads earlier chat summaries, and each concern's record without its evidence list.
Scratch stores; the brain and the concern door judge are fakes. Invented data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import brain_inbox, brain_step, conversations
from app.assistant.tests.concern_store_helpers import ScratchRegister
from belief_engine.intake.store import sqlite_file

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LATER = (NOW + timedelta(days=2)).astimezone().strftime("%Y-%m-%d %H:%M")   # a future local time


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(conversations, "_fetch_turns", lambda room, start, end: [])
    monkeypatch.setattr(conversations, "_describe", lambda room: f"About {room}.")
    monkeypatch.setattr(conversations, "_describe_account", lambda acct: f"Inbox of {acct}.")
    monkeypatch.setattr(conversations, "_load_email", lambda pod_id: {"account_id": "acct", "thread_id": "t1",
                                                                       "pod_id": pod_id})
    monkeypatch.setattr(conversations, "_email_thread", lambda acct, thread: [])
    monkeypatch.setattr(brain_step, "_room_history", lambda room, before: [
        {"when": "Mon 2026-09-28 20:00", "title": "flea medication plan", "body": "Talked about the dose.",
         "pod_id": "datapod:chat_cluster:x"}])
    monkeypatch.setattr(brain_step, "_message_text", lambda mid: f"(message {mid})")
    monkeypatch.setattr(brain_step, "_pod_text", lambda pid: f"(pod {pid})")
    monkeypatch.setattr(brain_step, "_calendar_lines", lambda now: [
        {"anchor": "calendar:bake", "text": "Bake sale @ Fri 08:00", "event": True}])
    from app.assistant.subconscious import work_links
    monkeypatch.setattr(work_links, "linked_work", lambda concern_ids, threads: [])
    monkeypatch.setattr(work_links, "similar_work", lambda texts, exclude=(): [])
    monkeypatch.setattr(work_links, "active_work", lambda: [])
    monkeypatch.setattr(work_links, "live_reminders", lambda now=None: [])
    from app.assistant.subconscious import kg_links
    monkeypatch.setattr(kg_links, "find_entities", lambda texts: [])
    monkeypatch.setattr(kg_links, "shared", lambda ids: {"links": [], "entities": []})
    monkeypatch.setattr(kg_links, "facts_by_meaning", lambda texts: [])
    from app.assistant.subconscious import belief_links
    monkeypatch.setattr(belief_links, "beliefs_by_meaning", lambda texts: [])


@pytest.fixture
def inbox(tmp_path):
    return sqlite_file(tmp_path / "inbox.db")


def _register(tmp_path, **buckets):
    return ScratchRegister(tmp_path).write({"schema_version": 1, "active": [], "addressing": [],
                                            "resolved": [], "dormant": [], **buckets})


def _concern(cid, title, **kw):
    return {"concern_id": cid, "title": title, "subject": "household", "kind": "anticipated_need",
            "severity": "medium", "horizon": "this_week", "done_when": "it is done", "notes": "n",
            "first_observed": (NOW - timedelta(hours=3)).isoformat(),
            "evidence": [{"kind": "chat_msg", "ref": "message:old", "snippet": "old"}], "reinforcement_notes": "", **kw}


def _event(inbox, i, minutes, text, *, route="new_matter", concerns=(), room="master_room", source="chat"):
    brain_inbox.ensure_schema(inbox)
    with inbox(True) as c:
        c.execute("INSERT INTO brain_events (source, source_ref, occurred_at, room_id, speaker, text, received_at, "
                  "gate_status, route, concern_ids) VALUES (?, ?, ?, ?, 'Owner', ?, ?, 'routed', ?, ?)",
                  (source, f"message:m{i}" if source == "chat" else f"datapod:email:{i}",
                   (NOW - timedelta(minutes=minutes)).isoformat(), room if source == "chat" else None, text,
                   NOW.isoformat(), route, json.dumps(list(concerns))))


def _brain(*answers):
    seen = []

    def call(payload):
        seen.append(payload)
        return answers[min(len(seen), len(answers)) - 1]
    call.seen = seen
    return call


def _all_new(payload):
    return {"decisions": [{"candidate": c["label"], "decision": "new", "same_as": None, "reason": "r"}
                          for c in payload["candidates"]]}


def test_events_group_into_matters(inbox):
    _event(inbox, 1, 50, "Vet moved to Friday.", route="concern", concerns=["c-vet"])
    _event(inbox, 2, 40, "Also bring the records.", route="concern", concerns=["c-vet"])
    _event(inbox, 3, 30, "Bake sale is Monday now.", room="slack/C1")
    _event(inbox, 4, 20, "Receipt", source="email")
    _event(inbox, 5, 10, "We should book the car service.", room="slack/C1")
    matters = brain_step.group_matters(brain_step.pending_events(inbox))
    assert [[e["text"] for e in m] for m in matters] == [
        ["Vet moved to Friday.", "Also bring the records."],
        ["Bake sale is Monday now.", "We should book the car service."],
        ["Receipt"]]


def test_a_note_lands_on_the_concern_and_the_events_are_consumed(inbox, tmp_path):
    reg = _register(tmp_path, active=[_concern("c-vet", "Vet appointment for the dog")])
    _event(inbox, 1, 10, "The vet moved us to Friday at 9.", route="concern", concerns=["c-vet"])
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1",
                                         "reason": "new date"}],
                    "concern_updates": [{"concern": "C1", "action": "note", "relies_on": ["E1"], "attach": ["E1"],
                                         "note": "The appointment moved to Friday 9:00.", "expires_at": LATER}],
                    "new_concerns": []})
    out = brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    assert out == {"matters": 1, "applied": 1, "failed": 0}
    [c] = reg.read()["active"]
    assert "[brain] The appointment moved to Friday 9:00." in c["reinforcement_notes"]
    assert c["evidence"][-1] == {"kind": "chat_msg", "ref": "message:m1", "snippet": "The vet moved us to Friday at 9."}
    assert "reinforcement_count" not in c, "a note is new facts, not disposition pressure"
    assert brain_step.pending_events(inbox) == []
    with inbox(False) as db:
        status, decisions = db.execute("SELECT status, decisions FROM brain_matters").fetchone()
    decided = json.loads(decisions)["events"]["message:m1"]
    assert status == "applied" and (decided["reason"], decided["concern_id"]) == ("new date", "c-vet")
    assert c["expires_at"] > NOW.isoformat(), "the update moved when it stops mattering"
    payload = brain.seen[0]
    assert payload["concerns"][0]["label"] == "C1" and "evidence" not in payload["concerns"][0]
    assert payload["room_history"][0]["summaries"][0]["title"] == "flea medication plan"
    assert "[E1] Owner: The vet moved us to Friday at 9." in payload["events"]


def test_a_resolution_closes_the_concern(inbox, tmp_path):
    reg = _register(tmp_path, active=[_concern("c-flea", "Flea medication due")])
    _event(inbox, 1, 10, "OK I have given the dogs their flea medication!", route="concern", concerns=["c-flea"])
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1",
                                         "reason": "done"}],
                    "concern_updates": [{"concern": "C1", "action": "resolve", "relies_on": ["E1"], "attach": ["E1"],
                                         "note": "The owner gave the dose."}]})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    after = reg.read()
    assert after["active"] == [] and after["resolved"][0]["resolution_reason"] == "The owner gave the dose."


def test_a_new_concern_carries_its_source_and_its_expiry(inbox, tmp_path):
    """Rule 6: where a concern came from, its own words copied by code (until 2026-10-09 the brain copied the
    owner's words into owner_words); rule 4: when it stops mattering."""
    reg = _register(tmp_path)
    _event(inbox, 1, 10, "Keep an eye on the passport renewal until it arrives.")
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "new_concern", "concern": "N1", "reason": "asked"}],
                    "new_concerns": [{"label": "N1", "title": "Passport renewal in progress", "subject": "owner",
                                      "kind": "anticipated_need", "severity": "medium", "horizon": "this_month",
                                      "done_when": "the renewed passport has arrived", "expires_at": LATER,
                                      "notes": "n", "relies_on": [], "attach": ["E1"]}]})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    [c] = reg.read()["active"]
    assert c["origin"] == "brain" and c["done_when"] == "the renewed passport has arrived"
    assert c["source"] == {"kind": "user", "at": (NOW - timedelta(minutes=10)).isoformat(), "ref": "message:m1",
                           "content": "Keep an eye on the passport renewal until it arrives."}
    from app.assistant.subconscious.concern_view import future_local_utc
    assert c["expires_at"] == future_local_utc(LATER, NOW).isoformat() and "owner_request" not in c


def test_an_invalid_answer_fails_the_matter_once_and_writes_nothing(inbox, tmp_path):
    reg = _register(tmp_path)
    _event(inbox, 1, 10, "Please remind me about the dentist.")
    bad = {"event_decisions": [{"event": "E1", "decision": "new_concern", "concern": "N1", "reason": "asked"}],
           "new_concerns": [{"label": "N1", "title": "Dentist", "kind": "anticipated_need", "severity": "low",
                             "horizon": "this_week", "done_when": "booked", "expires_at": "2020-01-01 09:00",
                             "notes": "n", "relies_on": [], "attach": ["E1"]}]}
    brain = _brain(bad, bad)
    out = brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    assert out["failed"] == 1 and len(brain.seen) == 2
    assert "N1: expires_at must be a future local YYYY-MM-DD HH:MM" in brain.seen[1]["correction"]
    assert reg.read()["active"] == []
    assert brain_step.pending_events(inbox) == [], "a failed matter is not retried automatically"
    with inbox(False) as db:
        assert db.execute("SELECT consumed_at FROM brain_events").fetchone()[0] is None


def test_an_event_changes_one_concern_and_an_update_names_only_its_own_events():
    """Rule 3. Until 2026-10-09 an event could be cited on several concerns, and "used" required a concern update, so
    a fact of the day was filed on whatever concern was nearest (a dog-walk follow-up on a friend's birthday)."""
    events = {"E1": {"text": "x"}, "E2": {"text": "y"}, "E3": {"text": "z"}}
    labels = {"E1": ["brain_event", "1"], "E2": ["brain_event", "2"], "E3": ["brain_event", "3"],
              "C1": ["concern", "c"], "C2": ["concern", "d"]}
    problems = brain_step._problems(
        {"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1", "reason": "r"},
                             {"event": "E2", "decision": "no_concern", "reason": "r"},
                             {"event": "E3", "decision": "no_concern", "concern": "C2", "reason": "r"}],
         "concern_updates": [{"concern": "C2", "action": "note", "relies_on": ["E2"], "attach": [], "note": "n"}]},
        events, ["C1", "C2"], labels, NOW)
    assert any("E1 changes C1, but no update of C1 records it" in p for p in problems)
    assert any("C2 is updated but no event is decided changes_concern for it" in p for p in problems)
    assert any("C2: ['E2'] are not decided to change it; an event changes one concern" in p for p in problems)
    assert any("E3: no_concern names no concern" in p for p in problems)
    fine = {"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1", "reason": "r"},
                                {"event": "E2", "decision": "no_concern", "reason": "r"},
                                {"event": "E3", "decision": "no_concern", "reason": "r"}],
            "concern_updates": [{"concern": "C1", "action": "note", "relies_on": ["E1"], "attach": ["E1"], "note": "n"}]}
    assert brain_step._problems(fine, events, ["C1", "C2"], labels, NOW) == []


def test_the_brain_prompt_renders_every_section():
    from app.assistant.dayflow_orchestrator import work_context
    payload = {"now": "Tue 2026-09-30 09:00", "events": "### Room master_room\nAbout.\n",
               "room_history": [{"description": "About master_room.", "summaries": [
                   {"when": "Mon", "title": "flea plan", "body": "Talked about the dose.", "pod_id": "p"}]}],
               "concerns": [brain_step._concern_view("C1", {**_concern(
                   "c1", "Flea medication due", expires_at="2026-09-30T23:00:00+00:00",
                   source={"kind": "user", "at": "2026-09-29T16:00:00+00:00", "ref": "message:m9",
                           "content": "track it"}), "_status": "active"})],
               "other_concerns": [{"label": "C2", "title": "Car service", "status": "active", "subject": "owner",
                                   "done_when": None}],
               "linked_work": [{"work_id": "w1", "title": "Bake cookies for the sale", "status": "done",
                                "objective": "Bake cookies for the sale", "created": "Mon", "updated": "Tue",
                                "ended_because": "", "link": "cites a concern of this matter", "score": None,
                                "outcomes": [{"title": "Bake the cookies", "outcome": "Two dozen baked.", "at": None}]}],
               "similar_work": [],
               "active_work": [{"work_id": "w2", "title": "Remind about the sale", "objective": "", "created": "Mon",
                                "progress": {"work_id": "w2", "state": "in_progress", "ended_at": None, "reason": None,
                                             "replanning": False, "done": [], "dropped": [], "unfinished": [],
                                             "to_come": [{"title": "Send the reminder", "wait": {"kind": "ready"}}]}}],
               "reminders": [{"ref": "reminder:r1", "title": "Bake cookies", "kind": "one time", "repeats": "",
                              "start": "Thu 18:00", "end": ""}],
               "calendar_lines": [{"anchor": "calendar:bake", "text": "Bake sale @ Fri 08:00", "event": True,
                                   "ref": "K1"}],
               "entities": [{"label": "Karjalohja", "description": "A place the family travels to."}],
               "shared": {"links": [{"type": "Event", "label": "Summer Trip", "start": "2019-07-01", "end": "",
                                     "description": "At the cottage."}], "entities": []},
               "beliefs": [{"belief_key": "B7", "statement": "Someone else sometimes walks the dogs.", "score": 0.4}],
               "correction": None}
    user = work_context._ENV.get_template("subconscious/brain/prompts/user.j2").render(agent_input=payload)
    assert "## Earlier in the chat room" in user and "About master_room." in user and "Talked about the dose." in user
    from app.assistant.utils.time_utils import local_time_text
    assert "### [C1] Flea medication due (active)" in user
    assert f'- raised from: the owner, {local_time_text("2026-09-29T16:00:00+00:00")}: "track it"' in user
    assert f"- no longer matters after: {local_time_text('2026-09-30T23:00:00+00:00')}" in user
    assert "evidence (" not in user
    assert "- [C2] Car service (active; about owner; done when: (not stated))" in user
    assert "### Bake cookies for the sale [w1; done; started Mon, last changed Tue; cites a concern of this matter]" in user
    assert '- task "Bake the cookies": Two dozen baked.' in user and "(none close enough)" in user
    assert "- Remind about the sale [w2] (started Mon)\n  In progress. The assistant carries this work" in user
    assert ("  Still to come in this work (the assistant does each step itself, as part of this work):\n"
            "    - Send the reminder: ready now; the assistant does it next") in user
    assert "- [reminder:r1] Bake cookies (one time; at Thu 18:00)" in user
    assert "- [K1] Bake sale @ Fri 08:00" in user and "calendar:bake" not in user
    assert "- Karjalohja: A place the family travels to." in user
    assert '- Event "Summer Trip" (2019-07-01): At the cottage.' in user
    assert "## What is believed about the household, closest in meaning" in user
    assert "- Someone else sometimes walks the dogs." in user and "B7" not in user
    system = work_context._ENV.get_template("subconscious/brain/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"}, resource_user_data={"first_name": "Sam"})
    assert "A concern whose source is Sam closes only when\nits done-when is met or Sam says it is over" in system
    assert "Every concern expires" in system and "owner_words" not in system
    assert "name in your note every item that depends on the old fact" in system


def test_a_work_report_the_brain_attaches_is_evidence_and_the_work_stays_where_it_was_made(inbox, tmp_path):
    """Until 2026-10-09 citing a work report on a concern attached the work to it (`attach_seen_work`), and a dog-walk
    follow-up landed on a friend's birthday. The report is the concern's evidence now; the work belongs to the concern
    its list entry was made for."""
    reg = _register(tmp_path, active=[_concern("c-vet", "Vet appointment for the dog")])
    brain_inbox.ensure_schema(inbox)
    with inbox(True) as c:
        c.execute("INSERT INTO brain_events (source, source_ref, occurred_at, speaker, text, received_at, gate_status, "
                  "route, concern_ids) VALUES ('work', 'work:work_cal:000-r1', ?, 'dayflow', ?, ?, 'routed', "
                  "'concern', ?)", (NOW.isoformat(), 'Work "Fix the calendar" (work_cal) ended: done.\n'
                                    'Objective: Clean up next week\'s calendar\nReason: the vet visit was moved to '
                                    'Friday 9:00 while cleaning up.', NOW.isoformat(), json.dumps(["c-vet"])))
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1",
                                         "reason": "the visit moved"}],
                    "concern_updates": [{"concern": "C1", "action": "note", "relies_on": ["E1"], "attach": ["E1"],
                                         "note": "The appointment is now Friday 9:00."}],
                    "new_concerns": []})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    [c] = reg.read()["active"]
    assert not c.get("attached_work"), "the work is not attached by judgment"
    assert c["evidence"][-1]["ref"] == "work:work_cal:000-r1" and c["evidence"][-1]["kind"] == "work"


def test_only_attach_attaches_and_references_are_stored_as_objects(inbox, tmp_path):
    """2026-10-08: citing a work report on a concern attached its work, and the Nest report cited on the pickup
    concern "to say it did not apply" attached the Nest work to it. Reading (relies_on) and attaching (attach) are
    separate fields; the decision keeps both as objects (kind, id), never labels."""
    reg = _register(tmp_path, active=[_concern("c-vet", "Vet appointment for the dog")])
    brain_inbox.ensure_schema(inbox)
    with inbox(True) as c:
        c.execute("INSERT INTO brain_events (source, source_ref, occurred_at, speaker, text, received_at, gate_status, "
                  "route, concern_ids) VALUES ('work', 'work:work_nest:000-r1', ?, 'dayflow', ?, ?, 'routed', "
                  "'concern', ?)", (NOW.isoformat(), 'Work "Set the AC" ended: done.', NOW.isoformat(),
                                    json.dumps(["c-vet"])))
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1",
                                         "reason": "the AC is set"}],
                    "concern_updates": [{"concern": "C1", "action": "note", "relies_on": ["E1"], "attach": [],
                                         "note": "The thermostat work ended; the visit is unchanged."}]})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    [c] = reg.read()["active"]
    assert not c.get("attached_work"), "read, not attached"
    from app.assistant.subconscious import relations
    with inbox(False) as db:
        matter_id, raw = db.execute("SELECT id, decisions FROM brain_matters").fetchone()
        [event_id] = [r[0] for r in db.execute("SELECT id FROM brain_events")]
    update = ("brain_update", f"{matter_id}:c-vet")
    assert relations.targets(update, "relies_on", connect=inbox) == [("brain_event", str(event_id))]
    assert relations.targets(update, "attach", connect=inbox) == []
    assert "relies_on" not in json.loads(raw)["concern_updates"][0], "one store: the links are relations"
    assert relations.sources(("brain_event", str(event_id)), connect=inbox) == [(update, "relies_on")]


def test_only_reference_fields_are_checked_never_the_words():
    """Owner, 2026-10-09: the prompt asks for words; scanning prose for labels is as good as a regex. Only the
    reference fields are checked."""
    events = {"E1": {"text": "x"}}
    labels = {"E1": ["brain_event", "1"], "C1": ["concern", "c"], "W2": ["work", "w"]}
    problems = brain_step._problems(
        {"event_decisions": [{"event": "E1", "decision": "changes_concern", "concern": "C1", "reason": "W2 ended"}],
         "concern_updates": [{"concern": "C1", "action": "note", "relies_on": ["E1"], "attach": ["W2", "C1"],
                              "note": "C1 remains open"}]}, events, ["C1"], labels, NOW)
    assert not any("written into" in p for p in problems)
    assert any("attach names events (E) only, got ['W2', 'C1']" in p for p in problems)
