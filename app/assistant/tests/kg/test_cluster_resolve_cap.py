"""kg_finding_cluster_resolve per-run cap (watchdog-risk fix, 2026-06-13).

Each candidate cluster is >= 1 LLM call; the routine had no cap and no
max_run_seconds, so a large pending-findings backlog was a graph-wide LLM
grind. The candidate builder excludes superseded findings, so resolved
clusters drop out — a per-run cluster cap converges over runs.

The candidate builder + resolver agent are mocked, so no DB/LLM.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_maintenance_pipeline import step_cluster_resolve as step


def _fake_candidates(n):
    # n primaries, each with 2 pending findings (>= 2 => a candidate cluster).
    return {f"node{i}": [{"id": f"f{i}a"}, {"id": f"f{i}b"}] for i in range(n)}


def _no_cluster_agent():
    # is_same_root=False => each cluster is examined but nothing is applied.
    return SimpleNamespace(
        action_handler=lambda msg: SimpleNamespace(data={"is_same_root": False}))


def test_cluster_resolve_caps_and_defers(monkeypatch):
    monkeypatch.setattr(step, "_build_candidate_clusters", lambda: _fake_candidates(5))
    monkeypatch.setattr(step.DI.agent_factory, "create_agent", lambda name: _no_cluster_agent())

    ctx = PipelineContext.for_date(pipeline_id="kg_finding_cluster_resolve")
    res = step.run(ctx, max_clusters=2)

    assert res["candidates_examined"] == 2   # only 2 clusters processed this run
    assert res["clusters_deferred"] == 3     # the rest deferred to next run


def test_cluster_resolve_unbounded_by_default(monkeypatch):
    monkeypatch.setattr(step, "_build_candidate_clusters", lambda: _fake_candidates(5))
    monkeypatch.setattr(step.DI.agent_factory, "create_agent", lambda name: _no_cluster_agent())

    ctx = PipelineContext.for_date(pipeline_id="kg_finding_cluster_resolve")
    res = step.run(ctx)  # no cap

    assert res["candidates_examined"] == 5 and res["clusters_deferred"] == 0
