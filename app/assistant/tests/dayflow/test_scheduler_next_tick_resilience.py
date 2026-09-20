"""The next-tick re-arm must be unkillable (scheduler audit S1, 2026-07-09).

``_arm_ceiling_tick`` is the SOLE place the next ordinary dayflow tick is armed, and it runs
in the tick's ``finally``. If it ever raises, the heartbeat goes dark — no tick scheduled AND
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
