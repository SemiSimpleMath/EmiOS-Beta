"""load_scope_for_source + kg_pipeline scope.yaml (Phase B template).

load_scope_for_source resolves a SOURCE's scope.yaml by (kind, id) and loads it
via the unified loader — the entry point for non-room sources (pipelines first).

Asserts (non-circular, hardcoded golden — per feedback_no_circular_characterization_tests):
- kg_pipeline scope.yaml loads to the intended permission: NO TOOLS (toolless
  extractors), resources [all] + chat/memory groups, authority 0, write_kg false.
- identity (owner_id/actor_id/surface) is stamped from the call, not the file.
- unknown kind / empty id / unwired kind raise loudly.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_scope_for_source.py
"""
from __future__ import annotations

import pytest

from app.assistant.scope.loader import load_scope_for_source


def _kg() :
    return load_scope_for_source(kind="pipeline", source_id="kg_pipeline", actor_id="resolve_messages")


def test_kg_pipeline_scope_loads_with_no_tools():
    s = _kg()
    assert s.tools.allowed_tools == []            # toolless extractors, fail-closed
    assert s.tools.per_manager == {}
    assert s.approval.authority_level == 0


def test_kg_pipeline_resources_faithful():
    s = _kg()
    assert s.resources.allowed_global_resources == ["all"]
    assert s.resources.resource_groups == ["chat", "memory"]


def test_kg_pipeline_writes_faithful():
    s = _kg()
    assert s.writes.write_unified_log is True
    assert s.writes.write_kg is False            # pipeline writes proposal tables, not live KG
    assert s.writes.allow_fact_extraction is False


def test_identity_stamped_from_call():
    s = _kg()
    assert s.owner_id == "kg_pipeline"
    assert s.actor_id == "resolve_messages"
    assert s.surface == "pipeline"


def test_identity_overrides_apply():
    s = load_scope_for_source(
        kind="pipeline", source_id="kg_pipeline", actor_id="segment_messages",
        identity_overrides={"actor_id": "override_actor", "scope_id": "scope::explicit"},
    )
    assert s.actor_id == "override_actor"
    assert s.scope_id == "scope::explicit"


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown source kind"):
        load_scope_for_source(kind="bogus", source_id="x", actor_id="a")


def test_empty_source_id_raises():
    with pytest.raises(ValueError, match="source_id must be non-empty"):
        load_scope_for_source(kind="pipeline", source_id="", actor_id="a")


def test_weekly_insights_scope_loads_with_no_tools():
    # Second migrated pipeline (toolless single-shot synthesizer).
    s = load_scope_for_source(kind="pipeline", source_id="weekly_insights", actor_id="weekly_synthesizer_runner")
    assert s.tools.allowed_tools == []
    assert s.approval.authority_level == 0
    assert s.resources.allowed_global_resources == ["all"]
    assert s.resources.resource_groups == ["chat", "memory"]
    assert s.writes.write_kg is False
    assert s.surface == "pipeline"
    assert s.owner_id == "weekly_insights"


def test_routine_with_no_scope_yaml_returns_none():
    # kind="routine" is the optional-attach model (wired in a05a6e0a): a routine
    # without a configs/routines/public/<id>.scope.yaml resolves to None, so the
    # tool runs scope-free (floor). Was previously a NotImplementedError before wiring.
    assert load_scope_for_source(kind="routine", source_id="some_routine", actor_id="a") is None


def test_missing_pipeline_scope_raises():
    with pytest.raises(FileNotFoundError):
        load_scope_for_source(kind="pipeline", source_id="nonexistent_pipeline_xyz", actor_id="a")
