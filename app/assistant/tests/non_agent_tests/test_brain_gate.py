"""Brain inbox + gate: every user message reaches the brain, routed against the open concerns.

2026-09-28: "OK I HAVE GIVEN [THE DOGS] THEIR FLEA MEDICATION!" (55 characters) was dropped
by the noticer's 60-character filter and the flea concern stayed live. These tests pin the new path:
messages land verbatim whatever their length, the gate's routing is validated and mapped back by
code, a routing failure still reaches the noticer, and the noticer reads the reports under the
concern they bear on. Scratch sqlite file; the gate model is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious import brain_inbox as inbox
from app.assistant.subconscious import gate
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
            "room_id": room, "speaker": "Owner", "text": text, "replying_to": None}


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
    assert "### c-meds — The cat's worm tablet is due soon [active]" in text
    assert "[message:m1]" in text and ": done!" in text
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
    payload = {"open_concerns": [{"label": "C1", "title": "The cat's worm tablet is due soon", "subject": "household",
                                  "status": "active", "notes": "Monthly."}],
               "events": [{"label": "E1", "time": "Tue 2026-03-10 10:55", "room": "master_room", "speaker": "Owner",
                           "text": "done!", "replying_to": "Did the cat get the tablet?"}],
               "correction": "not answered: ['E1']"}
    user = work_context._ENV.get_template("subconscious/gate/prompts/user.j2").render(agent_input=payload)
    assert "[C1] The cat's worm tablet is due soon" in user and "[E1]" in user and "done!" in user
    assert "(replying to the assistant: Did the cat get the tablet?)" in user
    assert "not answered: ['E1']" in user
    system = work_context._ENV.get_template("subconscious/gate/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"})
    assert "You route; the noticer judges." in system
