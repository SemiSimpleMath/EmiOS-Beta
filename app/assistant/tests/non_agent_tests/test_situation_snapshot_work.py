"""The situation auditor sees what the work did and what ended (2026-11-15).

The prompt audit of 2026-11-15 found the auditor told "No actions in last 4 hours" and "Recently Closed Items:
None" on a morning when work turned off the lights, sent tickets, completed and was abandoned: both sections read
dayflow action-log items, which stopped being written in 2026-06. And "Active session: 0.0 min" while the owner
was at the keyboard: the snapshot read keys the AFK monitor's reading never carries.

Pins: a step's result reaches the snapshot in the finalizer's words with its ledger act, under its work id; a work
object completed or dropped in the window shows with when and why; work outside the window does not; the session
length comes from the monitor's session start, and its absence is said in words. Invented data, in-memory store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.dayflow_orchestrator import state_store
from app.assistant.pipelines.dayflow.utils import situation_snapshot as ss
from work_objects.store import WorkStore

OUTCOME = "The porch light was switched off and the bridge confirmed it is off."


@pytest.fixture
def store():
    s = WorkStore(":memory:")
    yield s
    s.close()


def _finished_work(store):
    wo = store.apply("create_work_object", {"title": "Porch light off",
                                            "constraints": {"objective": "The porch light is off for the day."}})
    store.apply("add_node", {"work_id": wo.id, "id": "lights_off", "type": "subtask", "title": "Turn off the porch light",
                             "parent_id": wo.goal_node_id, "status": "dispatched"})
    store.apply("record_action", {"work_id": wo.id, "node_id": "lights_off", "channel": "ticket", "target": "user",
                                  "summary": "Porch light: turning it off now.", "outcome": "sent"}, actor="tool")
    store.apply("record_result", {"work_id": wo.id, "node_id": "lights_off", "expected_dispatch_epoch": 0,
                                  "evidence_id": "res", "title": "tool result", "answer": "1 device off",
                                  "status": "done"}, actor="tool")
    store.apply("finalize_task", {"work_id": wo.id, "node_id": "lights_off", "expected_dispatch_epoch": 0,
                                  "finalizer": {"verdict": "achieved", "outcome": OUTCOME, "next_step": ""}},
                actor="finalizer")
    return wo.id


def _dropped_work(store):
    wo = store.apply("create_work_object", {"title": "Ask about the bake sale",
                                            "constraints": {"objective": "The owner decides on the bake sale."}})
    store.apply("set_work_status", {"work_id": wo.id, "status": "abandoned",
                                    "reason": "The question expired unanswered; no further contact."}, actor="architect")
    return wo.id


def test_a_steps_result_and_its_act_reach_the_snapshot_under_its_work(store):
    wid = _finished_work(store)
    now = datetime.now(timezone.utc)
    block = ss._build_work_activity(ss._recent_work_objects(store, now), now)
    assert f"- {wid}: The porch light is off for the day." in block
    assert "ticket to user (sent): Porch light: turning it off now." in block
    assert f'step "Turn off the porch light", its result: {OUTCOME}' in block
    assert "achieved" not in block, "the finalizer's prose, never its verdict"


def test_work_ended_in_the_window_shows_with_when_and_why(store):
    done_id, dropped_id = _finished_work(store), _dropped_work(store)
    now = datetime.now(timezone.utc)
    block = ss._build_work_ended(ss._recent_work_objects(store, now), now)
    assert f"[{done_id}]" in block and "(completed " in block
    assert f"[{dropped_id}]" in block and "(dropped " in block
    assert "recorded reason: The question expired unanswered; no further contact." in block


def test_work_outside_the_window_is_not_shown(store):
    _finished_work(store)
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    recent = ss._recent_work_objects(store, later)
    assert recent == []
    assert ss._build_work_activity(recent, later).endswith("No step ran and nothing was sent.")
    assert ss._build_work_ended(recent, later).endswith("None.")


class _Monitor:
    def __init__(self, reading):
        self.reading = reading

    def get_computer_activity(self):
        return dict(self.reading)


def test_the_session_length_comes_from_the_monitors_session_start(monkeypatch):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=45)).isoformat()
    monkeypatch.setattr(DI, "afk_monitor", _Monitor({"is_afk": False, "active_start_utc": start}), raising=False)
    lines = ss._presence_lines(now)
    assert lines[0] == "**Computer:** AT KEYBOARD"
    assert lines[1].startswith("**Active session:** at the keyboard since ") and lines[1].endswith("(45 min)")


def test_a_missing_session_start_is_said_not_shown_as_zero(monkeypatch):
    monkeypatch.setattr(DI, "afk_monitor", _Monitor({"is_afk": False}), raising=False)
    lines = ss._presence_lines(datetime.now(timezone.utc))
    assert lines[1] == "**Active session:** the AFK monitor has not recorded when this session began."
    monkeypatch.setattr(DI, "afk_monitor", None, raising=False)
    assert ss._presence_lines(datetime.now(timezone.utc)) == ["**Computer:** unknown; the AFK monitor is not running."]


def test_a_chat_request_shows_its_result(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    items = [{"metadata": {"source_type": "master_room_dispatch", "state": "closed", "summary": "Find the shopping list.",
                           "dispatched_at": now, "executed_at": now, "execution_result": "Found it: hot dogs."}},
             {"metadata": {"source_type": "chat", "state": "artifact", "summary": "hello"}}]
    monkeypatch.setattr(state_store, "get_dayflow_items", lambda: items)
    block = ss._build_chat_requests()
    assert ": Find the shopping list.\n  result: Found it: hot dogs." in block
    assert "hello" not in block
