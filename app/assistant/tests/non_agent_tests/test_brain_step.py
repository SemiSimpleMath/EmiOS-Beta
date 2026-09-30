"""The brain step: what the gate passed on, read one matter at a time, applied to the concerns.

Pins: events group into matters (by the concerns they bear on, else by conversation or thread);
a note lands on the concern as a [brain] journal line with the events as evidence; a resolution
closes it; a new concern the owner asked for keeps the owner's words and must quote them exactly;
every event gets a decision that its citations agree with; an invalid answer gets one correction,
then the matter is recorded failed, nothing is written and it is not retried; the noticer cannot
close a concern the owner asked for; the brain reads earlier chat summaries and evidence as text.
Scratch stores; the brain and the concern door judge are fakes. Invented data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import brain_inbox, brain_step, conversations
from app.assistant.subconscious.persist import apply_noticer_output
from app.assistant.tests.concern_store_helpers import ScratchRegister
from belief_engine.intake.store import sqlite_file

NOW = datetime.now(timezone.utc).replace(microsecond=0)


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
    monkeypatch.setattr(brain_step, "_calendar", lambda now: "- [calendar:bake] Bake sale @ Fri 08:00")
    from app.assistant.subconscious import work_links
    monkeypatch.setattr(work_links, "linked_work", lambda concern_ids, threads: [])
    monkeypatch.setattr(work_links, "similar_work", lambda texts, exclude=(): [])
    monkeypatch.setattr(work_links, "active_work", lambda: [])
    monkeypatch.setattr(work_links, "live_reminders", lambda now=None: [])
    from app.assistant.subconscious import kg_links
    monkeypatch.setattr(kg_links, "find_entities", lambda texts: [])
    monkeypatch.setattr(kg_links, "shared", lambda ids: {"links": [], "entities": []})


@pytest.fixture
def inbox(tmp_path):
    return sqlite_file(tmp_path / "inbox.db")


def _register(tmp_path, **buckets):
    return ScratchRegister(tmp_path).write({"schema_version": 1, "active": [], "addressing": [],
                                            "resolved": [], "dormant": [], **buckets})


def _concern(cid, title, **kw):
    return {"concern_id": cid, "title": title, "subject": "household", "kind": "anticipated_need",
            "severity": "medium", "horizon": "this_week", "done_when": "it is done", "notes": "n",
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
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "used", "reason": "new date"}],
                    "concern_updates": [{"concern": "C1", "action": "note", "events": ["E1"],
                                         "note": "The appointment moved to Friday 9:00."}],
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
    assert status == "applied" and json.loads(decisions)["events"]["message:m1"]["reason"] == "new date"
    payload = brain.seen[0]
    assert payload["concerns"][0]["label"] == "C1" and payload["concerns"][0]["evidence"][0]["snippet"] == "(message old)"
    assert payload["room_history"][0]["summaries"][0]["title"] == "flea medication plan"
    assert "[E1] Owner: The vet moved us to Friday at 9." in payload["events"]


def test_a_resolution_closes_the_concern(inbox, tmp_path):
    reg = _register(tmp_path, active=[_concern("c-flea", "Flea medication due")])
    _event(inbox, 1, 10, "OK I have given the dogs their flea medication!", route="concern", concerns=["c-flea"])
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "used", "reason": "done"}],
                    "concern_updates": [{"concern": "C1", "action": "resolve", "events": ["E1"],
                                         "note": "The owner gave the dose."}]})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    after = reg.read()
    assert after["active"] == [] and after["resolved"][0]["resolution_reason"] == "The owner gave the dose."


def test_an_owner_request_becomes_a_concern_that_keeps_the_owner_words(inbox, tmp_path):
    reg = _register(tmp_path)
    _event(inbox, 1, 10, "Keep an eye on the passport renewal until it arrives.")
    brain = _brain({"event_decisions": [{"event": "E1", "decision": "tracked_as_new", "reason": "asked"}],
                    "new_concerns": [{"label": "N1", "title": "Passport renewal in progress", "subject": "owner",
                                      "kind": "anticipated_need", "severity": "medium", "horizon": "this_month",
                                      "done_when": "the renewed passport has arrived", "notes": "n", "events": ["E1"],
                                      "owner_words": "Keep an eye on the passport renewal until it arrives."}]})
    brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    [c] = reg.read()["active"]
    assert c["origin"] == "brain" and c["done_when"] == "the renewed passport has arrived"
    assert c["owner_request"]["words"] == "Keep an eye on the passport renewal until it arrives."
    assert c["owner_request"]["ref"] == "message:m1"


def test_an_invalid_answer_fails_the_matter_once_and_writes_nothing(inbox, tmp_path):
    reg = _register(tmp_path)
    _event(inbox, 1, 10, "Please remind me about the dentist.")
    bad = {"event_decisions": [{"event": "E1", "decision": "tracked_as_new", "reason": "asked"}],
           "new_concerns": [{"label": "N1", "title": "Dentist", "kind": "anticipated_need", "severity": "low",
                             "horizon": "this_week", "done_when": "booked", "notes": "n", "events": ["E1"],
                             "owner_words": "remind me about the dentist next week"}]}
    brain = _brain(bad, bad)
    out = brain_step.run_brain_step(connect=inbox, register_connect=reg.connect, call=brain, judge=_all_new)
    assert out["failed"] == 1 and len(brain.seen) == 2
    assert "owner_words must be copied exactly" in brain.seen[1]["correction"]
    assert reg.read()["active"] == []
    assert brain_step.pending_events(inbox) == [], "a failed matter is not retried automatically"
    with inbox(False) as db:
        assert db.execute("SELECT consumed_at FROM brain_events").fetchone()[0] is None


def test_decisions_must_agree_with_what_cites_the_events():
    events = {"E1": {"text": "x"}, "E2": {"text": "y"}}
    problems = brain_step._problems(
        {"event_decisions": [{"event": "E1", "decision": "used", "reason": "r"},
                             {"event": "E2", "decision": "not_worth_tracking", "reason": "r"}],
         "concern_updates": [{"concern": "C1", "action": "note", "events": ["E2"], "note": "n"}]},
        events, ["C1"])
    assert any("E1 is decided used but no concern update cites it" in p for p in problems)
    assert any("E2 is decided not_worth_tracking but" in p for p in problems)


def test_the_noticer_cannot_close_what_the_owner_asked_for(tmp_path):
    asked = _concern("c-pass", "Passport renewal", owner_request={"words": "watch it", "at": "t", "ref": "message:m1"})
    reg = _register(tmp_path, active=[asked])
    apply_noticer_output({"resolved_concerns": [{"concern_id": "c-pass", "reason": "no recent signal", "evidence": []}],
                          "concern_dispositions": [{"concern_id": "c-pass", "action": "accept_chronic", "reason": "r"}]},
                         connect=reg.connect, tick_log_path=reg.tick_log)
    [c] = reg.read()["active"]
    assert c["reinforcement_notes"].count("REFUSED a noticer") == 2


def test_the_brain_prompt_renders_every_section():
    from app.assistant.dayflow_orchestrator import work_context
    payload = {"now": "Tue 2026-09-30 09:00", "events": "### Room master_room\nAbout.\n",
               "room_history": [{"room": "master_room", "summaries": [
                   {"when": "Mon", "title": "flea plan", "body": "Talked about the dose.", "pod_id": "p"}]}],
               "concerns": [brain_step._concern_view("C1", {**_concern("c1", "Flea medication due",
                                                                       owner_request={"words": "track it"}),
                                                             "_status": "active"})],
               "other_concerns": [{"label": "C2", "title": "Car service", "status": "active", "subject": "owner",
                                   "done_when": None}],
               "linked_work": [{"work_id": "w1", "title": "Bake cookies for the sale", "status": "done",
                                "objective": "Bake cookies for the sale", "created": "Mon", "updated": "Tue",
                                "ended_because": "", "link": "cites a concern of this matter", "score": None,
                                "outcomes": [{"verdict": "achieved", "outcome": "Two dozen baked."}]}],
               "similar_work": [],
               "active_work": [{"work_id": "w2", "title": "Remind about the sale", "objective": "", "created": "Mon"}],
               "reminders": [{"ref": "reminder:r1", "title": "Bake cookies", "kind": "one time", "repeats": "",
                              "start": "Thu 18:00", "end": ""}],
               "calendar": "- [calendar:bake] Bake sale @ Fri 08:00",
               "entities": [{"label": "Karjalohja", "description": "A place the family travels to."}],
               "shared": {"links": [{"type": "Event", "label": "Summer Trip", "start": "2019-07-01", "end": "",
                                     "description": "At the cottage."}], "entities": []},
               "correction": None}
    user = work_context._ENV.get_template("subconscious/brain/prompts/user.j2").render(agent_input=payload)
    assert "## Earlier in master_room" in user and "Talked about the dose." in user
    assert "### [C1] Flea medication due (active)" in user and 'the owner asked for this: "track it"' in user
    assert "- [C2] Car service (active; about owner; done when: (not stated))" in user
    assert "### Bake cookies for the sale [w1; done; started Mon, last changed Tue; cites a concern of this matter]" in user
    assert "OUTCOME: Two dozen baked." in user and "(none close enough)" in user
    assert "- Remind about the sale [w2] (started Mon)" in user and "- [reminder:r1] Bake cookies (one time; at Thu 18:00)" in user
    assert "- [calendar:bake] Bake sale @ Fri 08:00" in user
    assert "- Karjalohja: A place the family travels to." in user
    assert '- Event "Summer Trip" (2019-07-01): At the cottage.' in user
    system = work_context._ENV.get_template("subconscious/brain/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"}, resource_user_data={"first_name": "Sam"})
    assert "closes only when its done-when is met or Sam says" in system
    assert "name in your note every item that depends on the old fact" in system
