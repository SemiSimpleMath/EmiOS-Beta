"""Routine audit R7 sweeps (2026-07-08).

Event-triggered fires now honor max_workers (the docstring claimed they
did; the code only checked in-flight-self), the admin UI's spec-default
for `enabled` matches the runtime's (True — a key-less routine RUNS),
and the empty _run_db_cleanup no-op task is gone.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from app.assistant.routine_manager.routine_manager import RoutineConfig, RoutineManager


def _event_mgr(monkeypatch, *, max_workers: int, active: int) -> tuple[RoutineManager, list]:
    m = RoutineManager.__new__(RoutineManager)
    m._lock = threading.Lock()
    m._running = set()
    m._active_threads = {f"busy{i}": object() for i in range(active)}
    routine = RoutineConfig(
        routine_id="cam", enabled=True, run_policy={},
        trigger={"type": "event", "topic": "snap"},
    )
    m._load_config = lambda: {"max_workers": max_workers}
    m._load_routines = lambda config: [routine]
    m._check_afk_guard = lambda r: (True, "afk ok")
    launched: list = []
    m._run_in_thread = lambda r, event_message=None: launched.append((r.routine_id, event_message))
    monkeypatch.setattr(
        "app.assistant.routine_manager.decision_log.record_skip_if_interesting",
        lambda *a, **kw: None,
    )
    return m, launched


def test_event_fire_skipped_at_worker_cap(monkeypatch):
    m, launched = _event_mgr(monkeypatch, max_workers=2, active=2)
    m._on_event_fire("cam", message="MSG")
    assert launched == []


def test_event_fire_runs_below_worker_cap(monkeypatch):
    m, launched = _event_mgr(monkeypatch, max_workers=2, active=1)
    m._on_event_fire("cam", message="MSG")
    assert launched == [("cam", "MSG")]


def test_enrich_routine_spec_default_enabled_matches_runtime():
    from app.routes.routines_admin import _enrich_routine

    enriched = _enrich_routine({"id": "keyless", "runner": "function"}, state={"routines": {}})
    assert enriched["enabled"] is True  # runtime _load_routines default


def test_background_manager_has_no_db_cleanup_noop():
    from app.assistant.background_task_manager.background_task_manager import (
        BackgroundTaskManager,
    )

    manager = BackgroundTaskManager()  # registers defaults, starts nothing
    assert "db_cleanup" not in manager.tasks
    assert {"watchdog", "ticket_maintenance", "routine_runner"} <= set(manager.tasks)
