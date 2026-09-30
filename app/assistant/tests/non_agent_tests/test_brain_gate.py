"""Brain inbox + gate: every user message reaches the brain, routed against the open concerns.

2026-09-28: "OK I HAVE GIVEN [THE DOGS] THEIR FLEA MEDICATION!" (55 characters) was dropped
by the noticer's 60-character filter and the flea concern stayed live. These tests pin the new path:
messages land verbatim whatever their length, the gate's routing is validated and mapped back by
code, a routing failure still reaches the brain, and every reader sees events inside their
conversations — chat by room, email by inbox and Gmail thread — under a line saying what each room
or inbox is, with the rest of the conversation or thread as unlabelled context. Scratch sqlite file; the gate model and the chat log are fakes. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_inbox as inbox
from app.assistant.subconscious import brain_step, conversations, gate
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)
REGISTER = {
    "active": [{"concern_id": "c-meds", "title": "The cat's worm tablet is due soon", "subject": "household",
                "notes": "Monthly tablet on the 12th."}],
    "addressing": [{"concern_id": "c-car", "title": "Car service needs booking", "subject": "owner"}],
    "resolved": [], "dormant": [],
}


def msg(i, minutes, text, room="master_room"):
    return {"id": f"m{i}", "timestamp": (NOW - timedelta(minutes=minutes)).replace(tzinfo=None),
            "room_id": room, "speaker": "Owner", "text": text}


LOG = []          # the fake chat log: the room's user and assistant turns


def turn(i, minutes, speaker, text, room="master_room"):
    return {"ref": f"message:{i}", "at": NOW - timedelta(minutes=minutes), "speaker": speaker,
            "text": text, "room": room}


@pytest.fixture(autouse=True)
def fake_log(monkeypatch):
    LOG.clear()
    monkeypatch.setattr(conversations, "_fetch_turns", lambda room, start, end: [
        t for t in sorted(LOG, key=lambda t: t["at"]) if t["room"] == room and start <= t["at"] <= end])
    monkeypatch.setattr(conversations, "_describe", lambda room: f"About {room}.")
    monkeypatch.setattr(conversations, "_describe_account", lambda acct: f"Inbox of {acct}.")
    monkeypatch.setattr(conversations, "_load_email", lambda pod_id: MAIL[pod_id])
    monkeypatch.setattr(conversations, "_email_thread", lambda acct, thread: [
        r for r in MAIL.values() if r["account_id"] == acct and r.get("thread_id") == thread])
    MAIL.clear()


MAIL = {}         # the fake email pods, by pod id


def mail(pod_id, minutes, subject, body, thread="t1", account="acct", sender="School Office", importance=7):
    MAIL[pod_id] = {"pod_id": pod_id, "account_id": account, "thread_id": thread, "subject": subject,
                    "importance": importance,
                    "body": body, "sender_display": sender, "sender_email": "office@school.example",
                    "received_at_utc": (NOW - timedelta(minutes=minutes)).isoformat(),
                    "created_at": (NOW - timedelta(minutes=minutes - 1)).isoformat()}
    return MAIL[pod_id]


@pytest.fixture
def connect(tmp_path):
    return sqlite_file(tmp_path / "inbox.db")


def test_every_user_message_lands_once_whatever_its_length(connect):
    batch = [msg(1, 30, "done!"), msg(2, 20, "Gave the cat the worm tablet just now."), msg(3, 10, "   ")]
    assert inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: batch) == 2
    assert inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: batch) == 0
    texts = [e["text"] for e in inbox.pending(connect)]
    assert texts == ["done!", "Gave the cat the worm tablet just now."]


def test_the_cursor_asks_only_for_what_is_new(connect):
    asked = []
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: asked.append(since) or [msg(1, 5, "hi")])
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: asked.append(since) or [])
    assert asked[0] == NOW - timedelta(hours=72)
    assert asked[1] == NOW - timedelta(minutes=5)


def _decisions(*rows):
    return {"decisions": [{"event": e, "route": r, "concerns": c, "reasoning": "r"} for e, r, c in rows]}


def test_routes_are_validated_and_mapped_back_by_code(connect):
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: [
        msg(1, 30, "Gave the cat the worm tablet."), msg(2, 20, "School trip form is due Friday."),
        msg(3, 10, "haha nice")])
    answers = iter([
        _decisions(("E1", "concern", ["C9"])),                      # bad label, missing events
        _decisions(("E1", "concern", ["C1"]), ("E2", "new_matter", []), ("E3", "none", [])),
    ])
    out = gate.run_gate(ingest=False, call=lambda payload: next(answers), register=REGISTER, connect=connect)
    assert out == {"ingested": 0, "routed": 3, "passed_on": 2, "failed": 0, "waiting": 0, "next_ready_at": None}
    reports = brain_step.pending_events(connect)
    assert [(r["text"], r["route"], r["concern_ids"]) for r in reports] == [
        ("Gave the cat the worm tablet.", "concern", ["c-meds"]),
        ("School trip form is due Friday.", "new_matter", [])]


def test_a_routing_failure_still_reaches_the_brain(connect):
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: [msg(1, 5, "Car booked for Tuesday.")])
    out = gate.run_gate(ingest=False, call=lambda payload: {"decisions": []}, register=REGISTER, connect=connect)
    assert out["failed"] == 1
    [r] = brain_step.pending_events(connect)
    assert r["gate_status"] == "failed" and "not answered" in r["gate_error"]


def test_nothing_pending_costs_nothing(connect):
    def no_call(payload):
        raise AssertionError("the gate model must not be called")
    assert gate.run_gate(ingest=False, call=no_call, register=REGISTER, connect=connect)["routed"] == 0


def test_gate_prompts_render_with_every_event_and_concern():
    from app.assistant.dayflow_orchestrator import work_context
    LOG.extend([turn("a1", 12, "assistant", "Did the cat get the tablet?")])
    event = {"source_ref": "message:m1", "occurred_at": (NOW - timedelta(minutes=5)).isoformat(),
             "room_id": "master_room", "speaker": "Owner", "text": "done!", "source": "chat"}
    payload = {**gate.build_payload([event], gate.open_concerns(REGISTER)), "correction": "not answered: ['E1']"}
    user = work_context._ENV.get_template("subconscious/gate/prompts/user.j2").render(agent_input=payload)
    assert "[C1] The cat's worm tablet is due soon" in user
    assert "### Room master_room\nAbout master_room." in user
    assert user.index("assistant: Did the cat get the tablet?") < user.index("[E1] Owner: done!")
    assert "not answered: ['E1']" in user and "Route every labelled event." in user
    system = work_context._ENV.get_template("subconscious/gate/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"})
    assert "You route; the noticer judges." in system


def _event(i, minutes, text, room="master_room", speaker="Owner"):
    return {"source": "chat", "source_ref": f"message:{i}",
            "occurred_at": (NOW - timedelta(minutes=minutes)).isoformat(),
            "room_id": room, "speaker": speaker, "text": text, "mark": f"E{i}"}


def _lines(rooms):
    return [(t["mark"], t["speaker"], t["text"]) for r in rooms for c in r["conversations"] for t in c["turns"]]


def test_events_sit_inside_their_whole_conversation_and_nothing_else():
    LOG.extend([
        turn("old", 3 * 24 * 60, "assistant", "Fair enough."),            # days earlier: another conversation
        turn("q0", 400, "Friend", "Morning!", room="slack/C1"),          # starts the conversation, 5h back:
        *[turn(f"q{k}", 400 - 20 * k, "Friend", f"chat {k}", room="slack/C1") for k in range(1, 19)],
        turn("mid", 120, "assistant", "Anything else?"),
        turn("aft", 55, "assistant", "Noted."),                          # after the event, same conversation
        turn("late", 10, "Owner", "unrelated later chat"),               # 45 min later: a new conversation
    ])
    rooms = conversations.build([_event(1, 60, "Vet moved to Friday."), _event(2, 30, "yes", room="slack/C1")],
                                now_utc=NOW)
    assert [r["heading"] for r in rooms] == ["Room master_room", "Room slack/C1"]     # by first event
    # 120 min back is more than the gap before the event at 60: its conversation holds only the
    # event and the reply after it; the days-old line and the later chat are other conversations.
    assert _lines(rooms[:1]) == [("E1", "Owner", "Vet moved to Friday."), (None, "assistant", "Noted.")]
    slack = _lines(rooms[1:])
    assert slack[0] == (None, "Friend", "Morning!") and slack[-1] == ("E2", "Owner", "yes")   # walked back 5h
    assert rooms[1]["description"] == "About slack/C1."


def test_silence_splits_conversations_and_dates_head_each_day():
    rooms = conversations.build([_event(1, 300, "first"), _event(2, 5, "second")], now_utc=NOW)
    convs = rooms[0]["conversations"]
    assert len(convs) == 2
    assert all(c["turns"][0]["day"] for c in convs) and convs[0]["turns"][0]["mark"] == "E1"


def test_an_event_without_a_room_or_a_room_without_a_description_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="has no room"):
        conversations.build([_event(1, 5, "hi", room=None)], now_utc=NOW)
    from app.assistant.rooms import room_resource_loader
    monkeypatch.setattr(conversations, "_describe", room_resource_loader.load_room_description)
    monkeypatch.setattr(room_resource_loader, "_read_frontmatter", lambda path: {"policy": {}})
    with pytest.raises(ValueError, match="no 'description'"):
        conversations.build([_event(1, 5, "hi")], now_utc=NOW)


def test_every_kept_email_lands_once_with_its_full_body(connect):
    first = mail("datapod:email:a", 90, "Bake sale Friday", "Please send two dozen cookies by Friday.")
    asked = []
    fetch = lambda since: asked.append(since) or [first]
    assert inbox.ingest_email(now_utc=NOW, connect=connect, fetch=fetch) == 1
    assert inbox.ingest_email(now_utc=NOW + timedelta(minutes=5), connect=connect, fetch=fetch) == 0
    assert asked == [NOW - timedelta(hours=72), NOW]           # cursor: the previous ingest time
    [e] = inbox.pending(connect)
    assert (e["source"], e["source_ref"], e["room_id"]) == ("email", "datapod:email:a", None)
    assert e["speaker"] == "School Office <office@school.example>"
    assert e["text"] == "Subject: Bake sale Friday\n\nPlease send two dozen cookies by Friday."
    assert e["occurred_at"] == (NOW - timedelta(minutes=90)).isoformat()


def test_an_email_is_read_inside_its_thread_under_its_inbox():
    mail("datapod:email:a", 3000, "Bake sale Friday", "Please send two dozen cookies.")
    mail("datapod:email:b", 60, "Re: Bake sale Friday", "Change of plan: the sale moved to Monday.")
    mail("datapod:email:c", 30, "Newsletter", "This week in the district.", thread="t2")
    events = [{"source": "email", "source_ref": pid, "occurred_at": MAIL[pid]["received_at_utc"],
               "room_id": None, "speaker": "School Office <office@school.example>",
               "text": f"Subject: {MAIL[pid]['subject']}\n\n{MAIL[pid]['body']}", "mark": f"E{i}"}
              for i, pid in enumerate(["datapod:email:b", "datapod:email:c"], 1)]
    [inbox_group] = conversations.build(events, now_utc=NOW)
    assert inbox_group["heading"] == "Email inbox acct" and inbox_group["description"] == "Inbox of acct."
    thread, single = inbox_group["conversations"]
    assert thread["label"] == "Email thread" and single["label"] == "Email"
    assert [(t["mark"], t["text"].splitlines()[0]) for t in thread["turns"]] == [
        (None, "Subject: Bake sale Friday"), ("E1", "Subject: Re: Bake sale Friday")]
    text = conversations.render([inbox_group])
    assert "### Email inbox acct\nInbox of acct." in text and "Change of plan: the sale moved to Monday." in text


def test_chat_and_email_share_one_view_and_an_unknown_source_is_refused():
    mail("datapod:email:a", 30, "Form due", "Sign the form.")
    email_event = {"source": "email", "source_ref": "datapod:email:a", "occurred_at": MAIL["datapod:email:a"]["received_at_utc"],
                   "room_id": None, "speaker": "School Office", "text": "Subject: Form due\n\nSign the form.", "mark": "E2"}
    groups = conversations.build([_event(1, 60, "Signed it already."), email_event], now_utc=NOW)
    assert [g["heading"] for g in groups] == ["Room master_room", "Email inbox acct"]
    with pytest.raises(ValueError, match="unknown source"):
        conversations.build([{**email_event, "source": "sms"}], now_utc=NOW)


# ── what reaches the gate, and when (owner, 2026-09-30) ────────────────────

def test_slack_and_low_importance_email_stay_out_of_the_brain(connect):
    batch = [msg(1, 30, "vet on Friday"), msg(2, 20, "lol nice", room="slack/C1")]
    assert inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: batch) == 1
    kept = mail("datapod:email:a", 60, "Form due", "Sign the form.", importance=6)
    dropped = mail("datapod:email:b", 50, "Sale ends today", "Subscribe now.", importance=5)
    assert inbox.ingest_email(now_utc=NOW, connect=connect, fetch=lambda since: [kept, dropped]) == 1
    assert [e["source_ref"] for e in inbox.pending(connect)] == ["datapod:email:a", "message:m1"]


def test_a_room_still_talking_waits_whole_and_email_goes_at_once(connect):
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: [
        msg(1, 30, "Car booked for Tuesday."),                         # master_room: quiet since 2 min ago
        msg(2, 2, "and the tyres too."),
        msg(3, 9, "The cat had the tablet.", room="tg_family")])       # tg_family: quiet 9 min
    inbox.ingest_email(now_utc=NOW, connect=connect, fetch=lambda since: [
        mail("datapod:email:a", 1, "Form due", "Sign the form.")])
    seen = []

    def call(payload):
        text = str(payload["conversations"])
        seen.append(text)
        labels = [f"E{i}" for i in range(1, text.count("'mark': 'E") + 1)]
        return _decisions(*[(e, "new_matter", []) for e in labels])

    out = gate.run_gate(ingest=False, call=call, register=REGISTER, connect=connect, now_utc=NOW)
    assert (out["routed"], out["waiting"]) == (2, 2)
    assert out["next_ready_at"] == NOW + timedelta(minutes=3)             # 2 min ago + 5 min quiet
    assert "The cat had the tablet." in seen[0] and "Form due" in seen[0]
    assert "Car booked" not in seen[0] and "tyres" not in seen[0]         # neither half of the talking room

    later = gate.run_gate(ingest=False, call=call, register=REGISTER, connect=connect,
                          now_utc=NOW + timedelta(minutes=3))
    assert (later["routed"], later["waiting"], later["next_ready_at"]) == (2, 0, None)
    assert "Car booked" in seen[1] and "tyres" in seen[1]


def test_the_wake_runs_on_events_and_register_changes_but_not_on_its_own_writes(monkeypatch):
    import threading
    from app.assistant.subconscious import brain_wake
    brain_wake._poked.clear()
    brain_wake.handle_envelope(object())
    assert brain_wake._poked.is_set()

    brain_wake._poked.clear()
    inside = []

    def own_run():
        brain_wake._inside.run = True
        brain_wake.poke()                                              # e.g. the brief writer saving a brief
        inside.append(brain_wake._poked.is_set())
    t = threading.Thread(target=own_run)
    t.start()
    t.join()
    assert inside == [False]


def test_work_reports_render_under_their_work_and_attached_work_under_its_concern(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_context
    event = {"source": "work", "source_ref": "work:work_a:000-r1", "occurred_at": (NOW - timedelta(minutes=3)).isoformat(),
             "room_id": None, "speaker": "dayflow", "mark": "E1",
             "text": 'Work "Check in" (work_a): the finalizer judged the task "Ask": achieved.'}
    text = conversations.render(conversations.build([event], now_utc=NOW))
    assert "### Work work_a" in text and '[E1] dayflow: Work "Check in"' in text
    monkeypatch.setattr(brain_step, "_evidence_text", lambda e: e.get("snippet") or "")
    concern = {"concern_id": "c1", "title": "Stress", "_status": "addressing", "evidence": [],
               "attached_work": {"work_a": {"work_id": "work_a", "title": "Check in", "objective": "Check in on stress",
                   "attached_at": "2026-03-10T10:00:00+00:00", "status": "done", "ended": {
                       "outcome": "done", "at": "2026-03-10T11:00:00+00:00", "reason": "judged complete"},
                   "judgments": [{"node_id": "ask", "title": "Ask", "verdict": "achieved", "next_step": "",
                                  "outcome": "He is less stressed.", "at": "2026-03-10T10:30:00+00:00",
                                  "replies": [{"question": "How is work?", "user_text": "less stress now"}]}]}}}
    c = brain_step._concern_view("C1", concern)
    rendered = work_context._ENV.from_string("{% include 'shared/brain/concern.j2' %}").render(c=c)
    assert "work attached 2026-03-10T10:00:00+00:00: work_a (done): Check in on stress" in rendered
    assert 'task "Ask" judged achieved' in rendered and "He is less stressed." in rendered
    assert 'said: "less stress now"' in rendered and "ended done" in rendered


def test_the_wake_sleeps_until_a_room_goes_quiet_or_a_hold_passes_whichever_is_first(monkeypatch):
    from app.assistant.subconscious import brain_wake, concern_brief, concern_handoff, concern_store
    quiet, hold = NOW + timedelta(minutes=4), NOW + timedelta(minutes=2)
    monkeypatch.setattr(gate, "run_gate", lambda: {"next_ready_at": quiet})
    monkeypatch.setattr(brain_step, "run_brain_step", lambda: {"matters": 0})
    monkeypatch.setattr(concern_brief, "run_briefs", lambda: {})
    monkeypatch.setattr(concern_handoff, "run_handoffs", lambda: {})
    monkeypatch.setattr(concern_store, "load_register", lambda: {})
    monkeypatch.setattr(concern_brief, "next_hold_at", lambda register, now: hold)
    assert brain_wake.run_brain()["next_wake_at"] == hold
    monkeypatch.setattr(concern_brief, "next_hold_at", lambda register, now: None)
    assert brain_wake.run_brain()["next_wake_at"] == quiet
    monkeypatch.setattr(gate, "run_gate", lambda: {"next_ready_at": None})
    assert brain_wake.run_brain()["next_wake_at"] is None
