"""Reliability spine R3: background no-silent-death + heartbeat.

Covers the heartbeat SSOT (record_tick ok/error, consecutive_errors increment+reset, snapshot shape)
and the DayflowScheduler's repeated-failure notice (fires exactly one owner ticket at the threshold
crossing, never below, and de-dupes above — the health surface carries the persistent count).
"""
from __future__ import annotations

import pytest

import app.services.scheduler_heartbeat as hb
from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler


@pytest.fixture(autouse=True)
def _reset_heartbeats():
    # Heartbeat state is a process-global registry; clear it before each test (autouse so it applies
    # to class methods too, which setup_function would not).
    hb.reset()
    yield


# ── heartbeat SSOT ────────────────────────────────────────────────
class TestHeartbeat:
    def test_ok_error_consecutive_and_reset(self):
        assert hb.record_tick("c", ok=True) == 0
        assert hb.record_tick("c", ok=False, error=RuntimeError("boom")) == 1
        assert hb.record_tick("c", ok=False) == 2
        assert hb.record_tick("c", ok=True) == 0          # success resets the streak
        snap = hb.get("c")
        assert snap["last_status"] == "ok"
        assert snap["consecutive_errors"] == 0
        assert snap["total_ticks"] == 4
        assert snap["total_errors"] == 2
        assert snap["last_ok_utc"] is not None

    def test_last_error_captured_then_cleared(self):
        hb.record_tick("c", ok=False, error=ValueError("the bad thing"))
        assert "the bad thing" in hb.get("c")["last_error"]
        hb.record_tick("c", ok=True)
        assert hb.get("c")["last_error"] is None

    def test_get_all_tracks_each_component(self):
        hb.record_tick("a", ok=True)
        hb.record_tick("b", ok=False)
        snap = hb.get_all()
        assert set(snap.keys()) == {"a", "b"}
        assert snap["b"]["last_status"] == "error"

    def test_get_unknown_is_none(self):
        assert hb.get("nope") is None


# ── dayflow no-silent-death ───────────────────────────────────────
class _FakeTicket:
    def __init__(self, tid):
        self.ticket_id = tid


class _FakeTM:
    def __init__(self):
        self.created = []
        self.proposed = []

    def create_ticket(self, **kw):
        self.created.append(kw)
        return _FakeTicket("t1")

    def mark_proposed(self, tid):
        self.proposed.append(tid)


class TestDayflowRepeatedFailureNotice:
    def _sched(self):
        return DayflowScheduler.__new__(DayflowScheduler)   # _maybe_notify uses no instance state

    def test_fires_once_at_threshold_and_dedupes(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        fake = _FakeTM()
        monkeypatch.setattr(DI, "ticket_manager", fake, raising=False)
        sched = self._sched()
        err = RuntimeError("dayflow exploded")

        # below threshold -> no ticket
        sched._maybe_notify_repeated_failure(consecutive_errors=2, error=err, run_id="r1")
        assert fake.created == []

        # exactly at threshold -> one ticket, marked proposed
        sched._maybe_notify_repeated_failure(consecutive_errors=3, error=err, run_id="r1")
        assert len(fake.created) == 1
        assert fake.created[0]["ticket_type"] == "dayflow_notify"
        assert fake.created[0]["trigger_reason"] == "dayflow_scheduler_repeated_tick_failure"
        assert fake.proposed == ["t1"]

        # above threshold -> de-duped (no second ticket)
        sched._maybe_notify_repeated_failure(consecutive_errors=4, error=err, run_id="r1")
        assert len(fake.created) == 1

    def test_no_ticket_manager_is_safe(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        monkeypatch.setattr(DI, "ticket_manager", None, raising=False)
        # Must not raise even at the threshold when there is no ticket manager.
        self._sched()._maybe_notify_repeated_failure(
            consecutive_errors=3, error=RuntimeError("x"), run_id="r1",
        )
