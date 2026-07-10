"""One-time reminder lifecycle (scheduler audit S2, 2026-07-09).

One-time events had no fire/expire lifecycle: a reminder due during a restart
that spanned its time was silently dropped (load_events skipped it — harsher
than the misfire grace, no catch-up, no notice), and fired/expired rows were
never deleted from time_events (a slow leak + a boot scan that grew without
bound). Now:

- one_time_expired_beyond_grace decides keep-vs-delete on load,
- a within-grace missed reminder is kept, fired late, and stamped 'overdue',
- a fired one-time event is deleted (delete-on-fire); intervals are not.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.scheduler.storage.event_storage import (
    ONE_TIME_CATCHUP_GRACE_SECONDS,
    one_time_expired_beyond_grace,
)
from app.assistant.scheduler.scheduler.timing_engine import TimingEngine


NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


class TestExpiryClassification:

    def test_future_one_time_is_not_expired(self):
        assert one_time_expired_beyond_grace(
            "one_time_event", NOW + timedelta(minutes=5), NOW) is False

    def test_recent_miss_within_grace_is_kept(self):
        assert one_time_expired_beyond_grace(
            "one_time_event", NOW - timedelta(seconds=ONE_TIME_CATCHUP_GRACE_SECONDS - 30), NOW) is False

    def test_old_miss_beyond_grace_is_expired(self):
        assert one_time_expired_beyond_grace(
            "one_time_event", NOW - timedelta(seconds=ONE_TIME_CATCHUP_GRACE_SECONDS + 30), NOW) is True

    def test_exactly_at_grace_is_kept(self):
        # Boundary: strictly greater-than expires, so at-grace is kept.
        assert one_time_expired_beyond_grace(
            "one_time_event", NOW - timedelta(seconds=ONE_TIME_CATCHUP_GRACE_SECONDS), NOW) is False

    def test_interval_event_is_never_expired_here(self):
        assert one_time_expired_beyond_grace("interval", NOW - timedelta(days=30), NOW) is False

    def test_missing_start_date_is_not_expired(self):
        assert one_time_expired_beyond_grace("one_time_event", None, NOW) is False


class FakeStorage:
    def __init__(self, events=None):
        self._events = dict(events or {})
        self.removed = []

    def get_time_event(self, event_id):
        return self._events.get(event_id)

    def remove_event(self, event_id, scheduler=None):
        self.removed.append(event_id)
        self._events.pop(event_id, None)
        return True

    def get_all_time_events(self):
        return list(self._events.values())


class FakeExecutor:
    def __init__(self):
        self.executed = []  # (event_id, payload snapshot at execute time)

    def execute(self, event):
        self.executed.append((event.event_id, dict(event.event_payload or {})))


class FakeApp:
    def app_context(self):
        return contextlib.nullcontext()


def _event(event_id, event_type, start_date, payload=None):
    return SimpleNamespace(
        event_id=event_id,
        event_type=event_type,
        start_date=start_date,
        event_payload=payload or {},
    )


@pytest.fixture
def engine():
    te = TimingEngine(event_storage=FakeStorage(), event_executor=FakeExecutor(), app=FakeApp())
    yield te
    te.shutdown()


class TestHandleTriggerLifecycle:

    def test_one_time_event_deleted_after_firing(self, engine):
        engine.event_storage = FakeStorage({"ev1": _event("ev1", "one_time_event", NOW.isoformat())})
        engine.executor = FakeExecutor()

        engine._handle_trigger("ev1")

        assert engine.executor.executed and engine.executor.executed[0][0] == "ev1"
        assert engine.event_storage.removed == ["ev1"], "one-time event must be deleted on fire"

    def test_interval_event_not_deleted(self, engine):
        engine.event_storage = FakeStorage({"iv1": _event("iv1", "interval", NOW.isoformat())})
        engine.executor = FakeExecutor()

        engine._handle_trigger("iv1")

        assert engine.executor.executed[0][0] == "iv1"
        assert engine.event_storage.removed == [], "interval events recur — never delete on fire"


class TestOverdueMarker:

    def test_on_time_fire_is_not_marked_overdue(self, engine):
        ev = _event("ev2", "one_time_event", datetime.now(timezone.utc).isoformat(), {"msg": "x"})
        engine.event_storage = FakeStorage({"ev2": ev})
        engine.executor = FakeExecutor()

        engine._handle_trigger("ev2")

        _id, payload = engine.executor.executed[0]
        assert "overdue" not in payload

    def test_late_fire_is_marked_overdue_before_execute(self, engine):
        # Caught up after a downtime → marked overdue, and the executor sees the stamped
        # payload (the marker is applied BEFORE execute), while the original data survives.
        late_start = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ev = _event("ev3", "one_time_event", late_start, {"msg": "take meds"})
        engine.event_storage = FakeStorage({"ev3": ev})
        engine.executor = FakeExecutor()

        engine._handle_trigger("ev3")

        _id, payload = engine.executor.executed[0]
        assert payload.get("overdue") is True
        assert payload.get("msg") == "take meds"
        assert payload.get("overdue_seconds", 0) >= 250  # ~5 min
        assert "scheduled_for" in payload
        assert engine.event_storage.removed == ["ev3"]  # still deleted on fire
