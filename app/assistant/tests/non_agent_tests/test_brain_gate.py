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
from types import SimpleNamespace

import pytest

from app.assistant.subconscious import brain_inbox as inbox
from app.assistant.subconscious import brain_step, conversations, gate
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)
REGISTER = {
    "active": [{"concern_id": "c-meds", "title": "The cat's worm tablet is due soon", "subject": "household",
                "notes": "Monthly tablet on the 12th.", "first_observed": "2026-03-10T14:00:00+00:00"}],
    "addressing": [{"concern_id": "c-car", "title": "Car service needs booking", "subject": "owner",
                    "first_observed": "2026-03-09T09:00:00+00:00"}],
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
    assert "### Chat room\nAbout master_room." in user and "master_room\n" not in user.split("About")[0]
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
    assert [r["heading"] for r in rooms] == ["Chat room", "Chat room"]                # by first event; no id
    assert [r["description"] for r in rooms] == ["About master_room.", "About slack/C1."]
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
    assert inbox_group["heading"] == "Email inbox" and inbox_group["description"] == "Inbox of acct."
    thread, single = inbox_group["conversations"]
    assert thread["label"] == "Email thread" and single["label"] == "Email"
    assert [(t["mark"], t["text"].splitlines()[0]) for t in thread["turns"]] == [
        (None, "Subject: Bake sale Friday"), ("E1", "Subject: Re: Bake sale Friday")]
    text = conversations.render([inbox_group])
    assert "### Email inbox\nInbox of acct." in text and "Change of plan: the sale moved to Monday." in text


def test_chat_and_email_share_one_view_and_an_unknown_source_is_refused():
    mail("datapod:email:a", 30, "Form due", "Sign the form.")
    email_event = {"source": "email", "source_ref": "datapod:email:a", "occurred_at": MAIL["datapod:email:a"]["received_at_utc"],
                   "room_id": None, "speaker": "School Office", "text": "Subject: Form due\n\nSign the form.", "mark": "E2"}
    groups = conversations.build([_event(1, 60, "Signed it already."), email_event], now_utc=NOW)
    assert [g["heading"] for g in groups] == ["Chat room", "Email inbox"]
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


def image_pod(pod_id, minutes, source_kind, text, camera="Front Door", **meta):
    return SimpleNamespace(pod_id=pod_id, body=text, one_liner=text, metadata={
        "source_kind": source_kind, "camera_name": camera,
        "occurred_at_utc": (NOW - timedelta(minutes=minutes)).isoformat(), **meta})


def test_flagged_camera_frames_reach_the_brain_and_raw_doorbell_events_stay_out(connect):
    pods = [image_pod("datapod:image:a", 40, "ring_doorbell_significant", "Two people carry a sofa to the door.",
                      significance_reason="A large delivery at the door."),
            image_pod("datapod:image:b", 30, "ring_doorbell_event", "Motion on the walkway."),
            image_pod("datapod:image:c", 20, "ring_bedroom_notable", "Awake with a screen on.", camera="Bedroom")]
    asked = []
    fetch = lambda since: asked.append(since) or pods
    assert inbox.ingest_camera(now_utc=NOW, connect=connect, fetch=fetch) == 2
    assert inbox.ingest_camera(now_utc=NOW + timedelta(minutes=5), connect=connect, fetch=fetch) == 0
    assert asked == [NOW - timedelta(hours=72), NOW]
    events = inbox.pending(connect)
    assert [(e["source"], e["source_ref"], e["speaker"]) for e in events] == [
        ("camera", "datapod:image:a", "Front Door"), ("camera", "datapod:image:c", "Bedroom")]
    assert events[0]["text"] == "Two people carry a sofa to the door.\n(flagged because: A large delivery at the door.)"
    assert inbox.ready(events, NOW) == events                         # a sighting goes at once


def test_sightings_render_under_their_camera_and_cite_their_pod():
    events = [{"source": "camera", "source_ref": f"datapod:image:{k}", "speaker": cam, "room_id": None,
               "occurred_at": (NOW - timedelta(minutes=m)).isoformat(), "text": text, "mark": f"E{i}"}
              for i, (k, cam, m, text) in enumerate([("a", "Front Door", 40, "A van parks."),
                                                     ("b", "Front Door", 30, "A parcel is left.")], 1)]
    [group] = conversations.build(events, now_utc=NOW)
    assert group["heading"] == "Camera Front Door" and "vision model" in group["description"]
    assert "[E2] Front Door: A parcel is left." in conversations.render([group])
    assert len(brain_step.group_matters([{**e, "id": i} for i, e in enumerate(events)])) == 1
    assert brain_step._evidence(events[0]) == {"kind": "pod", "ref": "datapod:image:a", "snippet": "A van parks."}


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
    assert "### Work\n" in text and '[E1] dayflow: Work "Check in"' in text, "the gate has no work labels"
    labelled = conversations.render(conversations.build([event], now_utc=NOW, work_labels={"work_a": "W1"}))
    assert "### Work [W1]" in labelled and "### Work work_a" not in labelled
    monkeypatch.setattr(brain_step, "_evidence_text", lambda e: e.get("snippet") or "")
    from app.assistant.subconscious import work_links
    monkeypatch.setattr(work_links, "progress", lambda work_id: {
        "work_id": work_id, "state": "done", "ended_at": "2026-03-10T11:00:00+00:00", "reason": "judged complete",
        "replanning": False, "dropped": [], "unfinished": [], "to_come": [],
        "done": [{"title": "Ask", "verdict": "achieved", "at": "2026-03-10T10:30:00+00:00"}]})
    concern = {"concern_id": "c1", "title": "Stress", "_status": "addressing", "evidence": [],
               "attached_work": {"work_a": {"work_id": "work_a", "title": "Check in", "objective": "Check in on stress",
                   "attached_at": "2026-03-10T10:00:00+00:00", "status": "done", "ended": {
                       "outcome": "done", "at": "2026-03-10T11:00:00+00:00", "reason": "judged complete"},
                   "judgments": [{"node_id": "ask", "title": "Ask", "verdict": "achieved", "next_step": "",
                                  "outcome": "He is less stressed.", "at": "2026-03-10T10:30:00+00:00",
                                  "replies": [{"question": "How is work?", "user_text": "less stress now"}]}]}}}
    c = brain_step._concern_view("C1", concern)
    rendered = work_context._ENV.from_string("{% include 'shared/brain/concern.j2' %}").render(c=c)
    from app.assistant.utils.time_utils import local_time_text
    assert f"work attached {local_time_text('2026-03-10T10:00:00+00:00')}: work_a: Check in on stress" in rendered
    assert f"  Ended {local_time_text('2026-03-10T11:00:00+00:00')}: done, every step finished." in rendered
    assert 'task "Ask" (' in rendered and "judged achieved" not in rendered and "He is less stressed." in rendered
    assert 'said: "less stress now"' in rendered


def test_the_wake_sleeps_until_a_room_goes_quiet_or_a_hold_passes_whichever_is_first(monkeypatch):
    from app.assistant.subconscious import (belief_occurrences, brain_wake, concern_brief, concern_handoff,
                                            concern_planner, concern_store, day_pass)
    quiet, hold = NOW + timedelta(minutes=4), NOW + timedelta(minutes=2)
    monkeypatch.setattr(day_pass, "run_day_pass", lambda: {"day_pass": "unchanged"})
    monkeypatch.setattr(belief_occurrences, "run_occurrence_handoffs", lambda: {"occurrences_handed": 0})
    monkeypatch.setattr(concern_planner, "run_planners", lambda **kw: {})
    from app.assistant.subconscious import brain_stories, day_tasks
    monkeypatch.setattr(brain_stories, "run_stories", lambda: {})
    monkeypatch.setattr(day_tasks, "run_day_tasks", lambda: {"day_tasks": "unchanged"})
    monkeypatch.setattr(gate, "run_gate", lambda: {"next_ready_at": quiet})
    monkeypatch.setattr(brain_step, "run_brain_step", lambda: {"matters": 0})
    from app.assistant.subconscious import brain_list
    monkeypatch.setattr(brain_list, "cancel_for_closed_concerns", lambda register, at: [])
    monkeypatch.setattr(concern_brief, "run_briefs", lambda: {})
    monkeypatch.setattr(concern_handoff, "run_handoffs", lambda: {})
    monkeypatch.setattr(concern_store, "load_register", lambda: {})
    monkeypatch.setattr(concern_planner, "next_hold_at", lambda register, now: hold)
    summary = brain_wake.run_brain()
    assert summary["next_wake_at"] == hold and summary["day_pass"] == "unchanged"
    monkeypatch.setattr(concern_planner, "next_hold_at", lambda register, now: None)
    assert brain_wake.run_brain()["next_wake_at"] == quiet
    monkeypatch.setattr(gate, "run_gate", lambda: {"next_ready_at": None})
    assert brain_wake.run_brain()["next_wake_at"] is None


@pytest.mark.parametrize("summary,expected", [
    ({"next_wake_at": None}, 600),                                  # nothing due: the heartbeat
    ({"next_wake_at": NOW + timedelta(hours=2)}, 600),              # due later than the heartbeat
    ({"next_wake_at": NOW + timedelta(minutes=2)}, 120),             # due sooner: wakes then
    (RuntimeError("boom"), 600),                                    # a failed run waits for the heartbeat too
])
def test_the_wake_never_sleeps_longer_than_the_heartbeat(monkeypatch, summary, expected):
    from app.assistant.subconscious import brain_wake

    def run():
        if isinstance(summary, Exception):
            raise summary
        return summary

    class Stop(Exception):
        pass
    waits = []

    def wait(timeout):
        waits.append(timeout)
        raise Stop
    monkeypatch.setattr(brain_wake, "run_brain", run)
    monkeypatch.setattr(brain_wake, "_poked", SimpleNamespace(clear=lambda: None, wait=wait))
    monkeypatch.setattr(brain_wake, "datetime", SimpleNamespace(now=lambda tz: NOW))
    with pytest.raises(Stop):
        brain_wake._loop()
    assert waits == [expected]


# ── presence: the owner leaving or coming back to the computer (2026-10-07) ──

def spell(i, start_min_ago, end_min_ago, provisional=False):
    return {"id": i, "start": NOW - timedelta(minutes=start_min_ago), "end": NOW - timedelta(minutes=end_min_ago),
            "provisional": provisional}


def test_an_absence_of_ten_minutes_or_more_reaches_the_brain_and_a_short_one_does_not(connect):
    spells = [spell(1, 300, 54), spell(2, 50, 35),             # a 4-minute gap: a coffee
              spell(3, 20, 12)]                                 # 15 minutes away, then gone again 12 minutes ago
    fetch = lambda start, end: spells
    assert inbox.ingest_presence(now_utc=NOW, connect=connect, fetch=fetch) == 2
    assert inbox.ingest_presence(now_utc=NOW + timedelta(minutes=5), connect=connect, fetch=fetch) == 0
    events = inbox.pending(connect)
    assert [(e["source"], e["source_ref"], e["speaker"], e["text"]) for e in events] == [
        ("presence", "presence:back:3", "computer", "Came back to the computer after 15 minutes away."),
        ("presence", "presence:away:3", "computer", "Left the computer: no keyboard or mouse use since."),
    ]
    assert [e["occurred_at"] for e in events] == [(NOW - timedelta(minutes=20)).isoformat(),
                                                  (NOW - timedelta(minutes=12)).isoformat()]
    assert inbox.ready(events, NOW) == events                          # presence goes at once


def test_an_absence_before_presence_intake_began_never_arrives(connect):
    """The first run reaches back an hour; a later run looks back a day but never before the first run's reach."""
    first = [spell(1, 600, 500), spell(2, 400, 50), spell(3, 30, 0, provisional=True)]   # back 30 minutes ago
    assert inbox.ingest_presence(now_utc=NOW, connect=connect, fetch=lambda s, e: first) == 1
    later = NOW + timedelta(hours=3)
    spells = [*first[:2], spell(3, 30, -60), spell(4, -100, -180, provisional=True)]   # away 40 minutes, back
    assert inbox.ingest_presence(now_utc=later, connect=connect, fetch=lambda s, e: spells) == 1
    assert [e["source_ref"] for e in inbox.pending(connect)] == ["presence:back:3", "presence:back:4"]
    assert inbox.pending(connect)[1]["text"] == "Came back to the computer after 40 minutes away."


def test_an_absence_is_due_for_the_brain_when_it_reaches_ten_minutes():
    away = [spell(1, 60, 4)]
    assert inbox.next_presence_due(now_utc=NOW, fetch=lambda s, e: away) == NOW + timedelta(minutes=6)
    assert inbox.next_presence_due(now_utc=NOW, fetch=lambda s, e: [spell(1, 60, 12)]) is None    # already an event
    assert inbox.next_presence_due(now_utc=NOW, fetch=lambda s, e: [spell(1, 60, 0, provisional=True)]) is None
    assert inbox.next_presence_due(now_utc=NOW, fetch=lambda s, e: []) is None


def test_presence_renders_as_its_own_group_and_cites_itself():
    events = [{"source": "presence", "source_ref": "presence:away:3", "speaker": "computer", "room_id": None,
               "occurred_at": (NOW - timedelta(minutes=40)).isoformat(),
               "text": "Left the computer: no keyboard or mouse use since.", "mark": "E1"}]
    [group] = conversations.build(events, now_utc=NOW)
    assert group["heading"] == "Presence at the computer" and "10 minutes or more" in group["description"]
    assert "[E1] computer: Left the computer" in conversations.render([group])
    assert len(brain_step.group_matters([{**e, "id": i} for i, e in enumerate(events)])) == 1
    assert brain_step._evidence(events[0])["kind"] == "presence"


def test_coming_back_wakes_the_brain_and_leaving_waits_for_the_timer(monkeypatch):
    from app.assistant.subconscious import brain_wake
    monkeypatch.setattr(brain_wake._inside, "run", False, raising=False)   # not the wake's own thread
    brain_wake._poked.clear()
    brain_wake.handle_afk_state_changed(SimpleNamespace(content="afk"))
    assert not brain_wake._poked.is_set()
    brain_wake.handle_afk_state_changed(SimpleNamespace(content="active"))
    assert brain_wake._poked.is_set()
    brain_wake._poked.clear()


# ── a concern whose day has ended (2026-10-08) ────────────────────────────────

def _day_concern(cid, raised, horizon="today"):
    return {"concern_id": cid, "title": cid, "horizon": horizon, "first_observed": raised}


def test_a_concern_whose_day_has_ended_reaches_the_brain_once_per_day(connect):
    # NOW is 2026-03-10 18:00 UTC; local day boundary is 05:00.
    reg = {"active": [_day_concern("c-pickup", "2026-03-09T03:00:00+00:00"),       # the evening before
                      _day_concern("c-today", "2026-03-10T17:00:00+00:00"),        # raised today: not ended
                      _day_concern("c-week", "2026-03-01T17:00:00+00:00", "this_week")],
           "addressing": []}
    assert inbox.add_day_ended_events(reg, now_utc=NOW, connect=connect) == 1
    assert inbox.add_day_ended_events(reg, now_utc=NOW + timedelta(minutes=10), connect=connect) == 0   # once a day
    with connect(False) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM brain_events")]
    assert [(r["source"], r["gate_status"], r["route"], r["concern_ids"]) for r in rows] == [
        ("time", "routed", "concern", '["c-pickup"]')]
    assert "has ended" in rows[0]["text"]
    # the next day: the pickup is asked again, and the concern raised on the 10th has ended too
    assert inbox.add_day_ended_events(reg, now_utc=NOW + timedelta(days=1), connect=connect) == 2
    [group] = conversations.build([{**rows[0], "mark": "E1"}], now_utc=NOW)
    assert group["heading"] == "The clock"
    assert brain_step._evidence(rows[0])["kind"] == "time"


def test_a_report_on_the_brain_s_own_work_says_where_it_came_from(tmp_path, monkeypatch):
    """2026-10-09: a report on work the brain's list made reached it as a new matter, and it raised a concern from
    the 6 AM thermostat routine that had succeeded and from an email still waiting for the owner's approval. The
    work's heading now says it was made from the brain's own list, and the brain's rule says what that means."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from app.assistant.subconscious import brain_list, db
    from belief_engine.intake.store import sqlite_file
    lst = sqlite_file(tmp_path / "l.db")
    monkeypatch.setattr(db, "connect", lst)
    brain_list.apply_calendar("2026-03-10", [{"label": "I1", "belief_key": "B3", "times": ["6:00 AM"],
                                              "statement": "AC at 75 in the morning."}],
                              [{"item": "I1", "decision": "needed", "assistant_part": "set the AC to 75",
                                "when": "6:00 AM", "covers": [], "why": "A standing wish."}],
                              cause="test", at=NOW, connect=lst)
    store = get_dayflow_work_store()
    made = store.apply("create_work_object", {"title": "Set the AC", "goal_content": "Set the AC",
                                              "satisfied_when_kind": "all_owned_children_done",
                                              "constraints": {"list_entry_id": "cal:2026-03-10:B3"}})
    other = store.apply("create_work_object", {"title": "Other", "goal_content": "Other",
                                               "satisfied_when_kind": "all_owned_children_done"})
    event = lambda wid: {"source": "work", "source_ref": f"work:{wid}:000-r1", "room_id": None, "speaker": "dayflow",
                         "occurred_at": NOW.isoformat(), "mark": "E1", "text": 'Work "Set the AC" ended: done.'}
    text = conversations.render(conversations.build([event(made.id)], now_utc=NOW))
    assert "This work was made from the brain's own list for Tuesday 2026-03-10: \"set the AC to 75\"." in text
    assert "own list" not in conversations.render(conversations.build([event(other.id)], now_utc=NOW))
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    system = _ENV.get_template("subconscious/brain/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"}, resource_user_data={"first_name": "Sam"})
    assert "is the outcome of a decision already made" in system
