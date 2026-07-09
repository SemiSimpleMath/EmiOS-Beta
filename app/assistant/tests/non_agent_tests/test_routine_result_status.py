"""Runner results now count (routine audit R2, 2026-07-08).

_dispatch_routine's return value used to be discarded — only a raised
exception marked a run failed. A pipeline whose steps failed without
raising (PipelineRunner catches step exceptions and returns
status='error') recorded SUCCESS, reset its failure streak, and never
reached backoff / auto-disable / ticket.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.routine_manager.routine_manager import _status_from_result
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult
from app.assistant.routine_manager.runners.pipeline_runner import PipelineRoutineRunner


# ── _status_from_result: the manager-side mapping ─────────────────


def test_reported_error_counts_as_failure():
    status, error = _status_from_result(
        RoutineRunResult(status="error", message="pipeline status=error failed_steps=['a']")
    )
    assert status == "error"
    assert "failed_steps" in error


def test_success_and_skipped_count_as_success():
    assert _status_from_result(RoutineRunResult(status="success")) == ("success", None)
    assert _status_from_result(RoutineRunResult(status="skipped")) == ("success", None)


def test_error_without_message_gets_a_default():
    status, error = _status_from_result(RoutineRunResult(status="error"))
    assert status == "error"
    assert "status=error" in error


# ── pipeline runner: explicit status contract ─────────────────────


def _ctx() -> RoutineRunContext:
    now = datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc)
    return RoutineRunContext(run_id="r1", now_utc=now, now_local=now)


def _pipeline_routine() -> SimpleNamespace:
    return SimpleNamespace(
        routine_id="t", runner="pipeline", spec={"pipeline_id": "p"}, notes=None,
    )


def _run_with_pipeline_returning(monkeypatch, payload):
    fake = SimpleNamespace(run=lambda **kw: payload)
    monkeypatch.setattr(
        "app.assistant.routine_manager.runners.pipeline_runner.resolve_pipeline",
        lambda pid: fake,
    )
    return PipelineRoutineRunner().run(_pipeline_routine(), _ctx())


def test_pipeline_error_status_maps_to_error_result(monkeypatch):
    result = _run_with_pipeline_returning(
        monkeypatch,
        {"status": "error", "failed_steps": ["duplicate_scan"], "steps": {}},
    )
    assert result.status == "error"
    assert "duplicate_scan" in result.message


def test_pipeline_success_status_maps_to_success(monkeypatch):
    result = _run_with_pipeline_returning(monkeypatch, {"status": "success", "steps": {}})
    assert result.status == "success"


def test_pipeline_missing_status_key_fails_loud(monkeypatch):
    with pytest.raises(RuntimeError, match="no 'status' key"):
        _run_with_pipeline_returning(monkeypatch, {"run_id": "x", "steps": {}})


def test_pipeline_non_dict_return_fails_loud(monkeypatch):
    with pytest.raises(RuntimeError, match="the pipeline contract"):
        _run_with_pipeline_returning(monkeypatch, None)


# ── registered pipelines honor the contract ───────────────────────


def test_dayflow_and_kg_maintenance_declare_status_in_return():
    """The two formerly statusless pipelines now return an explicit
    top-level status (source-shape guard, no execution)."""
    import inspect

    from app.assistant.pipelines.dayflow import step_runner as dayflow_sr
    from app.assistant.pipelines.kg_maintenance_pipeline import pipeline as kgm

    assert '"status": overall_status' in inspect.getsource(dayflow_sr.DayFlowRunner.run_once)
    assert 'summary["status"]' in inspect.getsource(kgm.KGMaintenancePipeline.run)
