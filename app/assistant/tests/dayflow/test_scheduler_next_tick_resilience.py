"""The next-tick re-arm must be unkillable (scheduler audit S1, 2026-07-09).

`_schedule_next_from_items` is the SOLE place the next dayflow tick is armed,
and it runs in the tick's `finally`. It previously `raise`d on a malformed
`reactivate_at_utc` (and on any scan error), which skipped the ceiling-tick
re-arm below it — leaving the heartbeat dark until an external poke, which
re-hit the same bad item and re-halted. Now a bad item is skipped and any
scan failure still arms a ceiling tick, so the scan can never leave the
scheduler with no next tick scheduled.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.assistant.dayflow_orchestrator.dayflow_scheduler import (
    MAX_CEILING_SECONDS,
    MIN_GAP_SECONDS,
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


def _patch_items(monkeypatch, items):
    # get_meta is identity here — each "item" IS its meta dict.
    monkeypatch.setattr(
        "app.assistant.dayflow_orchestrator.dayflow_scheduler.get_meta", lambda item: item
    )
    monkeypatch.setattr(
        "app.assistant.dayflow_orchestrator.state_store.load_existing_dayflow_items",
        lambda: items,
    )


class TestNextTickResilience:

    def test_malformed_timestamp_is_skipped_and_ceiling_armed(self, monkeypatch):
        # A single unparseable reactivate_at_utc must not halt the scan: the bad
        # item is skipped and the ceiling tick is still armed.
        _patch_items(monkeypatch, [
            {"state": "waiting", "reactivate_at_utc": "not-a-date", "item_id": "bad1"},
        ])
        s, fake = _make_scheduler()
        before = datetime.now(timezone.utc)

        s._schedule_next_from_items()  # must NOT raise

        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["args"][0] == "ceiling"
        delay = (call["run_date"] - before).total_seconds()
        assert MAX_CEILING_SECONDS - 5 <= delay <= MAX_CEILING_SECONDS + 5

    def test_good_item_still_scheduled_despite_malformed_sibling(self, monkeypatch):
        # A valid item alongside a malformed one still drives its own timer — the
        # bad sibling doesn't abort the scan before the good one is considered.
        good_at = datetime.now(timezone.utc) + timedelta(seconds=300)
        _patch_items(monkeypatch, [
            {"state": "waiting", "reactivate_at_utc": "garbage", "item_id": "bad1"},
            {"state": "waiting", "reactivate_at_utc": good_at.isoformat(), "item_id": "good1"},
        ])
        s, fake = _make_scheduler()

        s._schedule_next_from_items()

        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["args"][0] == "item_timer"
        assert call["args"][1] == "good1"
        delay = (call["run_date"] - datetime.now(timezone.utc)).total_seconds()
        assert MIN_GAP_SECONDS <= delay <= 300 + 5

    def test_scan_failure_still_arms_ceiling(self, monkeypatch):
        # If the scan itself throws (e.g. the item store raises), the heartbeat is
        # still guaranteed — a ceiling tick is armed instead of the method raising
        # out of the tick's finally.
        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.dayflow_scheduler.get_meta", lambda item: item
        )

        def _boom():
            raise RuntimeError("item store unavailable")

        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.state_store.load_existing_dayflow_items", _boom
        )
        s, fake = _make_scheduler()
        before = datetime.now(timezone.utc)

        s._schedule_next_from_items()  # must NOT propagate

        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["args"][0] == "ceiling_after_scan_error"
        delay = (call["run_date"] - before).total_seconds()
        assert MAX_CEILING_SECONDS - 5 <= delay <= MAX_CEILING_SECONDS + 5

    def test_clean_items_unaffected(self, monkeypatch):
        # Sanity: with only valid items the earliest still wins as before.
        soon = datetime.now(timezone.utc) + timedelta(seconds=200)
        later = datetime.now(timezone.utc) + timedelta(seconds=900)
        _patch_items(monkeypatch, [
            {"state": "waiting", "reactivate_at_utc": later.isoformat(), "item_id": "later1"},
            {"state": "waiting", "reactivate_at_utc": soon.isoformat(), "item_id": "soon1"},
        ])
        s, fake = _make_scheduler()

        s._schedule_next_from_items()

        assert len(fake.add_calls) == 1
        assert fake.add_calls[0]["args"][1] == "soon1"
