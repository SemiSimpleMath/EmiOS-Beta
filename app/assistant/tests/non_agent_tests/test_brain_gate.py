"""Brain inbox + gate: every user message reaches the brain, routed against the open concerns.

2026-09-28: "OK I HAVE GIVEN [THE DOGS] THEIR FLEA MEDICATION!" (55 characters) was dropped
by the noticer's 60-character filter and the flea concern stayed live. These tests pin the new path:
messages land verbatim whatever their length, the gate's routing is validated and mapped back by
code, a routing failure still reaches the noticer, and every reader sees events inside their
conversations — chat by room, email by inbox and Gmail thread — under a line saying what each room
or inbox is, with the rest of the conversation or thread as unlabelled context. Scratch sqlite file; the gate model and the chat log are fakes. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_inbox as inbox
from app.assistant.subconscious import conversations, gate
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


def mail(pod_id, minutes, subject, body, thread="t1", account="acct", sender="School Office"):
    MAIL[pod_id] = {"pod_id": pod_id, "account_id": account, "thread_id": thread, "subject": subject,
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
    triggered = []
    out = gate.run_gate(ingest=False, call=lambda payload: next(answers), register=REGISTER,
                        trigger=triggered.append, connect=connect)
    assert out == {"ingested": 0, "routed": 3, "passed_on": 2, "failed": 0}
    assert triggered, "a passed-on event triggers a noticer tick"
    reports = inbox.unconsumed_reports(connect)
    assert [(r["text"], r["route"], r["concern_ids"]) for r in reports] == [
        ("Gave the cat the worm tablet.", "concern", ["c-meds"]),
        ("School trip form is due Friday.", "new_matter", [])]


def test_a_routing_failure_still_reaches_the_noticer(connect):
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: [msg(1, 5, "Car booked for Tuesday.")])
    out = gate.run_gate(ingest=False, call=lambda payload: {"decisions": []}, register=REGISTER,
                        trigger=lambda reason: None, connect=connect)
    assert out["failed"] == 1
    [r] = inbox.unconsumed_reports(connect)
    assert r["gate_status"] == "failed" and "not answered" in r["gate_error"]


def test_nothing_pending_costs_nothing(connect):
    def no_call(payload):
        raise AssertionError("the gate model must not be called")
    assert gate.run_gate(ingest=False, call=no_call, register=REGISTER, connect=connect)["routed"] == 0


def test_the_noticer_reads_reports_under_their_concern_then_forgets_them(connect):
    inbox.ingest_chat(now_utc=NOW, connect=connect, fetch=lambda since: [msg(1, 5, "done!")])
    gate.run_gate(ingest=False, call=lambda p: _decisions(("E1", "concern", ["C1"])), register=REGISTER,
                  trigger=lambda reason: None, connect=connect)
    reports = inbox.unconsumed_reports(connect)
    concerns = {c["concern_id"]: {**c, "_bucket": b} for b in ("active", "addressing") for c in REGISTER[b]}
    text = inbox.render_reports(reports, concerns)
    assert "### Room master_room\nAbout master_room." in text
    assert "[message:m1] Owner: done!" in text
    assert "(gate: bears on The cat's worm tablet is due soon [active] (c-meds))" in text
    # No decision: the report comes back next tick.
    assert inbox.mark_consumed(reports, [], connect) == ["message:m1"]
    assert len(inbox.unconsumed_reports(connect)) == 1
    inbox.mark_consumed(reports, [{"ref": "message:m1", "decision": "used", "reason": "settles it"}], connect)
    assert inbox.unconsumed_reports(connect) == []
    with connect(False) as c:
        assert tuple(c.execute("SELECT noticer_decision, noticer_reason FROM brain_events").fetchone()) == (
            "used", "settles it")
    assert inbox.render_reports([], concerns) == "(no new reports since your last tick)"


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
