"""The next-tick re-arm must be unkillable (scheduler audit S1, 2026-07-09).

``_arm_ceiling_tick`` runs after readiness skips and in an executed tick's ``finally``. If it ever raises, the heartbeat goes dark — no tick scheduled AND
the work-node re-arm that follows the call site is skipped — and autonomy ends until an
external poke arrives.

Until 2026-09-16 this method also scanned dayflow ITEMS for the earliest
``reactivate_at_utc`` and armed a fast tick for that item, which is where its original
fragility lived (a single malformed timestamp used to abort the scan and skip the re-arm).
The item dispatch lane is retired — everything dispatched is a work node, and precise wakes
are armed by ``_arm_work_node_wakes`` — so the scan is gone and only the guarantee remains.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.assistant.dayflow_orchestrator.dayflow_scheduler import (
    MAX_CEILING_SECONDS,
    DayflowScheduler,
)


class FakeJob:
    def __init__(self, run_date):
        self.next_run_time = run_date


class FakeAPScheduler:
    def get_jobs(self):
        return []

    def __init__(self):
        self.jobs = {}
        self.add_calls = []

    def add_job(self, func=None, trigger=None, run_date=None, args=None, id=None,
                replace_existing=None, misfire_grace_time=None):
        self.jobs[id] = FakeJob(run_date)
        self.add_calls.append({"id": id, "run_date": run_date, "args": args})

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


def _make_scheduler():
    fake = FakeAPScheduler()
    s = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=fake), app=None)
    s._started = True
    return s, fake


class TestNextTickResilience:

    def test_ceiling_tick_is_armed(self):
        s, fake = _make_scheduler()
        before = datetime.now(timezone.utc)

        s._arm_ceiling_tick()

        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["args"][0] == "ceiling"
        delay = (call["run_date"] - before).total_seconds()
        assert MAX_CEILING_SECONDS - 5 <= delay <= MAX_CEILING_SECONDS + 5

    def test_scheduler_failure_does_not_propagate(self, monkeypatch):
        """The heartbeat re-arm runs inside the tick's finally. If the underlying
        scheduler throws, it must log and return — raising here would also skip the
        work-node re-arm on the next line and end autonomy silently."""
        s, fake = _make_scheduler()

        def _boom(**kwargs):
            raise RuntimeError("apscheduler unavailable")

        monkeypatch.setattr(fake, "add_job", _boom)

        s._arm_ceiling_tick()  # must NOT propagate

        assert fake.add_calls == []

    def test_no_item_state_is_consulted(self, monkeypatch):
        """The re-arm no longer reads the dayflow item store at all. If it ever does
        again, this fails — the item lane is not coming back through the scheduler."""
        def _boom(*args, **kwargs):
            raise AssertionError("the ceiling re-arm must not read dayflow items")

        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.state_store.load_existing_dayflow_items", _boom
        )
        s, fake = _make_scheduler()

        s._arm_ceiling_tick()

        assert len(fake.add_calls) == 1
        assert fake.add_calls[0]["args"][0] == "ceiling"



class TestReadinessSkips:
    @staticmethod
    def _gate(monkeypatch, *, setup_ready, has_history):
        from contextlib import nullcontext
        from unittest.mock import Mock
        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.dayflow_scheduler.setup_complete",
            lambda: setup_ready)
        session = Mock()
        session.query.return_value.limit.return_value.scalar.return_value = 1 if has_history else None
        manager = SimpleNamespace(read_session=lambda: nullcontext(session))
        get_db = Mock(return_value=manager)
        monkeypatch.setattr("app.models.db_manager.get_db_manager", get_db)
        # Readiness skips must not invoke the orchestrator or arm task execution.
        cadence = Mock(side_effect=AssertionError("not ready to run agents"))
        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.dayflow_tick.dayflow_orchestrator_cadence_tick", cadence)
        return get_db, cadence

    def test_setup_incomplete_keeps_fallback_without_reading_history(self, monkeypatch):
        get_db, cadence = self._gate(monkeypatch, setup_ready=False, has_history=False)
        s, fake = _make_scheduler()
        before = datetime.now(timezone.utc)
        s._execute_tick("startup")
        assert len(fake.add_calls) == 1
        assert MAX_CEILING_SECONDS - 5 <= (fake.add_calls[0]['run_date'] - before).total_seconds() <= MAX_CEILING_SECONDS + 5
        get_db.assert_not_called()
        cadence.assert_not_called()
        assert not s._running

    def test_empty_history_rearms_each_consumed_check(self, monkeypatch):
        get_db, cadence = self._gate(monkeypatch, setup_ready=True, has_history=False)
        s, fake = _make_scheduler()
        for expected in (1, 2):
            fake.jobs.clear()  # APScheduler consumes a date job before its callback.
            before = datetime.now(timezone.utc)
            s._execute_tick("ceiling")
            assert len(fake.add_calls) == expected
            assert MAX_CEILING_SECONDS - 5 <= (fake.add_calls[-1]['run_date'] - before).total_seconds() <= MAX_CEILING_SECONDS + 5
        assert get_db.call_count == 2
        cadence.assert_not_called()

    def test_readiness_skip_preserves_sooner_external_wake(self, monkeypatch):
        self._gate(monkeypatch, setup_ready=False, has_history=False)
        s, fake = _make_scheduler()
        s._schedule_tick(delay_seconds=60, reason="external")
        original = fake.add_calls[0]
        s._execute_tick("startup")
        assert fake.add_calls == [original]

    def test_stopped_scheduler_does_not_rearm(self, monkeypatch):
        get_db, cadence = self._gate(monkeypatch, setup_ready=False, has_history=False)
        s, fake = _make_scheduler()
        s._started = False
        s._execute_tick("startup")
        assert fake.add_calls == []
        get_db.assert_not_called()
        cadence.assert_not_called()
