"""KG pipeline status maps onto the routine contract (2026-07-09).

The kg_pipeline runner's terminal status `completed_idle` (drained cleanly,
nothing left to do) is a SUCCESS, but it was leaking straight through as the
pipeline's contract status. The routine runner treats anything but
success/skipped as a FAILED run, so three clean daily drains auto-disabled the
routine (last error "pipeline status=completed_idle"). KGPipeline.run now maps
the runner's terminal status onto the contract vocabulary.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _fake_runner_result(status):
    return SimpleNamespace(
        pipeline_id="kg_pipeline", run_id="r1", date="2026-07-09",
        status=status, workers=[{"step": "s", "errors": 0}], halt_reason=None, halt_step=None,
    )


@pytest.fixture
def kg_pipeline(monkeypatch):
    import app.assistant.pipelines.kg_pipeline.pipeline as kgp
    # subsystem flag on (imported inside run) + lightweight context so run() reaches the runner.
    monkeypatch.setattr("app.assistant.utils.subsystem_flags.is_subsystem_enabled", lambda name: True)
    monkeypatch.setattr(kgp, "PipelineContext", SimpleNamespace(for_date=lambda **k: SimpleNamespace()))
    return kgp.KGPipeline()


@pytest.mark.parametrize("runner_status,expected", [
    ("completed_idle", "success"),   # the bug: this was auto-disabling the routine
    ("stopped", "success"),          # externally cancelled — not a failure
    ("halted", "error"),             # a worker halted — a real failure
    ("weird_unknown", "error"),      # unknown terminal status — fail loud
])
def test_runner_status_maps_to_contract(kg_pipeline, monkeypatch, runner_status, expected):
    monkeypatch.setattr(kg_pipeline._runner, "run", lambda *a, **k: _fake_runner_result(runner_status))
    out = kg_pipeline.run()
    assert out["status"] == expected
    assert out["runner_status"] == runner_status  # raw status preserved for observability


def test_routine_layer_treats_noncontract_status_as_error():
    # Confirms WHY the leak mattered: the routine runner maps any status outside
    # success/skipped to an error run (which drives backoff/auto-disable). So the
    # pipeline MUST normalize completed_idle -> success at its own boundary.
    from app.assistant.routine_manager.routine_manager import _status_from_result
    assert _status_from_result(SimpleNamespace(status="success", message=""))[0] == "success"
    assert _status_from_result(SimpleNamespace(status="skipped", message=""))[0] == "success"
    assert _status_from_result(SimpleNamespace(status="completed_idle", message=""))[0] == "error"
