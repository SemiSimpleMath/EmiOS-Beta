"""The work-progress follow-up wake — chains advance in minutes, not one step per ceiling tick.

When a node reaches a result (worker done/failed, or a reply recorded), node_dispatch publishes
`dayflow_work_progress`; the scheduler follows up with a NON-poke wake so only the small MIN_GAP
floor applies (a poke would sit behind the 10-minute throttle). Mid-tick signals are flagged and
consumed at tick end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.assistant.dayflow_orchestrator.dayflow_scheduler import (
    DEBOUNCE_SECONDS,
    MIN_GAP_SECONDS,
    POKE_MIN_INTERVAL_SECONDS,
    DayflowScheduler,
)


class FakeJob:
    def __init__(self, run_date):
        self.next_run_time = run_date


class FakeAPScheduler:
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


class TestWorkProgressFollowup:

    def test_idle_progress_schedules_prompt_tick(self):
        s, fake = _make_scheduler()
        before = datetime.now(timezone.utc)
        s._on_work_progress(message=None)
        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["args"][0] == "work_progress"
        delay = (call["run_date"] - before).total_seconds()
        assert delay < POKE_MIN_INTERVAL_SECONDS, "must not sit behind the 10-min poke throttle"
        assert delay <= DEBOUNCE_SECONDS + 5

    def test_midtick_progress_flags_followup_without_scheduling(self):
        s, fake = _make_scheduler()
        s._running = True
        s._on_work_progress(message=None)
        assert s._followup_requested is True
        assert fake.add_calls == []

    def test_recent_run_applies_min_gap_floor_not_poke_throttle(self):
        s, fake = _make_scheduler()
        now = datetime.now(timezone.utc)
        s._last_run_finished_utc = now
        s._on_work_progress(message=None)
        run_date = fake.add_calls[0]["run_date"]
        delay = (run_date - now).total_seconds()
        assert MIN_GAP_SECONDS - 5 <= delay <= MIN_GAP_SECONDS + 10
        assert delay < POKE_MIN_INTERVAL_SECONDS

    def test_sooner_scheduled_run_is_kept(self):
        s, fake = _make_scheduler()
        soon = datetime.now(timezone.utc) + timedelta(seconds=30)
        fake.jobs["dayflow_scheduler_next_tick"] = FakeJob(soon)
        s._on_work_progress(message=None)
        # The guard keeps the sooner job — no replacement scheduled.
        assert fake.add_calls == []
        assert fake.jobs["dayflow_scheduler_next_tick"].next_run_time == soon
