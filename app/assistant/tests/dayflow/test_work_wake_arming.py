"""Work-node time-wake arming is soonest-first (scheduler audit S3, 2026-07-09).

_arm_work_node_wakes re-arms one precise APScheduler job per time-gated work
node on every tick. It's capped at _MAX_WORK_WAKES; previously it armed in
work-store iteration order (updated_at desc), so past the cap the most-imminent
wake could go unarmed until the next tick. Now candidates are sorted by wake_at
and the soonest win the cap. Under the cap, all are armed (no behavior change);
a past wake is armed for ~now.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler


class FakeAPScheduler:
    def __init__(self):
        self.add_calls = []

    def add_job(self, func=None, trigger=None, run_date=None, args=None, id=None,
                replace_existing=None, misfire_grace_time=None):
        self.add_calls.append({"id": id, "run_date": run_date, "args": args})

    def get_job(self, job_id):
        return None


class FakeNode:
    def __init__(self, node_id, wake_at, wake_kind="time", status="waiting"):
        self.id = node_id
        self.wake_at = wake_at
        self.wake_kind = wake_kind
        self.status = status


class FakeWO:
    def __init__(self, wo_id, nodes):
        self.id = wo_id
        self.nodes = {n.id: n for n in nodes}


class FakeStore:
    def __init__(self, wos, statuses=None):
        self._wos = {wo.id: wo for wo in wos}
        self._statuses = statuses or {}

    def list_work_objects(self):
        return [{"id": wo.id, "status": self._statuses.get(wo.id, "active")} for wo in self._wos.values()]

    def load(self, wo_id):
        return self._wos[wo_id]


def _make_scheduler():
    fake = FakeAPScheduler()
    s = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=fake), app=None)
    s._started = True
    return s, fake


def _patch_store(monkeypatch, store):
    monkeypatch.setattr(
        "app.assistant.dayflow_orchestrator.work_store.get_dayflow_work_store", lambda: store
    )


def _armed_node_ids(fake):
    return {call["args"][1] for call in fake.add_calls}


class TestWorkWakeArming:

    def test_soonest_wakes_win_the_cap(self, monkeypatch):
        monkeypatch.setattr(
            "app.assistant.dayflow_orchestrator.dayflow_scheduler._MAX_WORK_WAKES", 2
        )
        now = datetime.now(timezone.utc)
        store = FakeStore([
            FakeWO("wo1", [
                FakeNode("far", now + timedelta(seconds=900)),
                FakeNode("near", now + timedelta(seconds=100)),
            ]),
            FakeWO("wo2", [
                FakeNode("mid", now + timedelta(seconds=300)),
            ]),
        ])
        _patch_store(monkeypatch, store)
        s, fake = _make_scheduler()

        s._arm_work_node_wakes()

        # Cap = 2 → the two soonest (near @100s, mid @300s) win; far @900s is dropped.
        assert _armed_node_ids(fake) == {"near", "mid"}

    def test_all_armed_under_cap(self, monkeypatch):
        now = datetime.now(timezone.utc)
        store = FakeStore([
            FakeWO("wo1", [
                FakeNode("a", now + timedelta(seconds=300)),
                FakeNode("b", now + timedelta(seconds=100)),
                FakeNode("c", now + timedelta(seconds=900)),
            ]),
        ])
        _patch_store(monkeypatch, store)
        s, fake = _make_scheduler()

        s._arm_work_node_wakes()

        assert _armed_node_ids(fake) == {"a", "b", "c"}

    def test_past_wake_armed_for_now(self, monkeypatch):
        now = datetime.now(timezone.utc)
        store = FakeStore([FakeWO("wo1", [FakeNode("late", now - timedelta(seconds=100))])])
        _patch_store(monkeypatch, store)
        s, fake = _make_scheduler()

        s._arm_work_node_wakes()

        run_date = fake.add_calls[0]["run_date"]
        # A past wake_at is armed just ahead of now, not left in the past.
        assert run_date > now
        assert (run_date - now).total_seconds() < 10

    def test_ineligible_nodes_and_wos_skipped(self, monkeypatch):
        now = datetime.now(timezone.utc)
        store = FakeStore(
            [
                FakeWO("live", [
                    FakeNode("ready", now + timedelta(seconds=100)),
                    FakeNode("not_time", now + timedelta(seconds=100), wake_kind="event"),
                    FakeNode("running", now + timedelta(seconds=100), status="in_progress"),
                    FakeNode("no_wake", None),
                ]),
                FakeWO("finished", [FakeNode("done_node", now + timedelta(seconds=100))]),
            ],
            statuses={"finished": "done"},
        )
        _patch_store(monkeypatch, store)
        s, fake = _make_scheduler()

        s._arm_work_node_wakes()

        assert _armed_node_ids(fake) == {"ready"}
