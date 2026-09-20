"""Stopping and holding a timed wake must update the actual scheduler jobs."""
from types import SimpleNamespace
from unittest.mock import Mock
from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler, JOB_ID, _WORK_WAKE_JOB_PREFIX
from app.assistant.tests.dayflow.test_wake_pass_is_targeted_and_serialized import _scheduler, _ready_node, _store, _Invoker


def test_stop_removes_main_and_work_jobs_but_keeps_other_jobs():
    jobs = {JOB_ID, _WORK_WAKE_JOB_PREFIX + "wo::task", "unrelated-job"}
    timer = SimpleNamespace(get_jobs=lambda: [SimpleNamespace(id=n) for n in jobs], remove_job=lambda nid: jobs.remove(nid))
    scheduler = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=timer), app=None)
    scheduler._started = True
    scheduler.stop()
    assert jobs == {"unrelated-job"}


def test_a_callback_after_stop_cannot_invoke_a_manager(monkeypatch):
    ref = _ready_node(_store())
    invoker = _Invoker(hold=0)
    scheduler, _ = _scheduler(monkeypatch, invoker)
    scheduler._started = False
    scheduler._fire_work_node(*ref.split("::"))
    assert not invoker.calls


def test_a_wake_pass_rearms_precise_jobs(monkeypatch):
    ref = _ready_node(_store())
    invoker = _Invoker(hold=0)
    scheduler, _ = _scheduler(monkeypatch, invoker)
    rearm = Mock()
    monkeypatch.setattr(scheduler, "_arm_work_node_wakes", rearm)
    scheduler._fire_work_node(*ref.split("::"))
    rearm.assert_called_once_with()


def test_stopped_scheduler_cannot_add_a_tick():
    timer = SimpleNamespace(get_job=lambda nid: None, add_job=Mock())
    scheduler = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=timer), app=None)
    scheduler._schedule_tick(delay_seconds=1, reason="late callback")
    timer.add_job.assert_not_called()


def test_failed_wake_waits_before_retrying(monkeypatch):
    ref = _ready_node(_store())
    invoker = _Invoker(hold=0)
    scheduler, _ = _scheduler(monkeypatch, invoker)
    monkeypatch.setattr(invoker, "invoke", Mock(side_effect=RuntimeError("manager failed")))
    rearm = Mock()
    monkeypatch.setattr(scheduler, "_arm_work_node_wakes", rearm)
    scheduler._fire_work_node(*ref.split("::"))
    assert rearm.call_args.kwargs["min_due_delay_seconds"] >= 60


def test_scheduler_event_subscription_is_idempotent(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    registered = set()
    def register(topic, handler):
        assert (topic, handler) not in registered
        registered.add((topic, handler))
    hub = SimpleNamespace(register_event=register, unregister_event=lambda topic, handler: registered.remove((topic, handler)))
    monkeypatch.setattr(DI, "event_hub", hub)
    scheduler = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=Mock()), app=None)
    scheduler._subscribe_events()
    scheduler._subscribe_events()
    assert len(registered) == 4


def test_partial_subscription_failure_can_be_retried(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    registered = set()
    fail = [True]
    def register(topic, handler):
        if topic == "afk_state_changed" and fail[0]:
            raise RuntimeError("subscription failed")
        assert (topic, handler) not in registered
        registered.add((topic, handler))
    hub = SimpleNamespace(register_event=register, unregister_event=lambda topic, handler: registered.remove((topic, handler)))
    monkeypatch.setattr(DI, "event_hub", hub)
    scheduler = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=Mock()), app=None)
    import pytest
    with pytest.raises(RuntimeError):
        scheduler._subscribe_events()
    assert not registered
    fail[0] = False
    scheduler._subscribe_events()
    assert len(registered) == 4
