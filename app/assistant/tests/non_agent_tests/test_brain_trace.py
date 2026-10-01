"""Recording the brain's model calls for the /brain page.

Pins: a traced agent's call is stored with the system and user prompts as sent, the result, the
engine and the tags of the block it ran in (tags nest and unwind); an error is stored instead of a
result; untraced agents are not stored; calls are found by event and by concern; a failed write is
logged, never raised into the call. Scratch store; invented data only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.assistant.subconscious import brain_trace
from belief_engine.intake.store import sqlite_file

MESSAGES = [{"role": "system", "content": "You route events."},
            {"role": "user", "content": [{"type": "text", "text": "[E1] done!"}, {"type": "image_path", "path": "x"}]}]


@pytest.fixture
def connect(tmp_path):
    return sqlite_file(tmp_path / "trace.db")


def _record(connect, agent="subconscious::gate", **kw):
    brain_trace.record_call(agent_name=agent, messages=MESSAGES, engine="gpt-x",
                            started=datetime.now(timezone.utc), connect=connect, **kw)


def test_a_call_is_stored_as_sent_with_its_tags(connect):
    with brain_trace.trace(stage="brain", event_ids=[4, 5]):
        with brain_trace.trace(concern_id="c-1"):
            _record(connect, agent="subconscious::brain", result={"event_decisions": []})
        _record(connect, agent="subconscious::concern_door", result={"decisions": []})
    _record(connect, result={"decisions": []})
    calls = brain_trace.list_calls(connect=connect)
    assert [c["agent"] for c in calls][::-1] == ["subconscious::brain", "subconscious::concern_door", "subconscious::gate"]
    by_agent = {c["agent"]: c for c in calls}
    assert by_agent["subconscious::brain"]["trace"] == {"stage": "brain", "event_ids": [4, 5], "concern_id": "c-1"}
    assert by_agent["subconscious::concern_door"]["trace"] == {"stage": "brain", "event_ids": [4, 5]}
    assert by_agent["subconscious::gate"]["trace"] == {}
    full = brain_trace.get_call(by_agent["subconscious::brain"]["id"], connect=connect)
    assert full["system"] == "You route events." and full["user"] == "[E1] done!\n[image_path]"
    assert full["result"] == {"event_decisions": []} and full["engine"] == "gpt-x"
    assert [c["agent"] for c in brain_trace.calls_for_events([5], connect=connect)] == [
        "subconscious::brain", "subconscious::concern_door"]


def test_an_error_is_stored_and_untraced_agents_are_not(connect):
    _record(connect, error=ValueError("still invalid"))
    _record(connect, agent="kg_team::planner", result={"x": 1})
    [call] = brain_trace.list_calls(connect=connect)
    assert call["error"] == "ValueError: still invalid"
    assert brain_trace.get_call(call["id"], connect=connect)["result"] is None


def test_calls_are_found_by_concern_and_steward_calls_by_item(connect):
    with brain_trace.trace(stage="brief", concern_id="c-bake"):
        _record(connect, agent="subconscious::brief", result={"what": "w"})
    brain_trace.record_call(agent_name="dayflow_orchestrator::strategic_planner_wo",
                            messages=[{"role": "user", "content": "- [concern_handoff:c-bake:abc] Handed over"}],
                            engine="gpt-x", started=datetime.now(timezone.utc), result={}, connect=connect)
    assert len(brain_trace.calls_for_concern("c-bake", connect=connect)) == 1
    assert len(brain_trace.steward_calls_mentioning("concern_handoff:c-bake:abc", connect=connect)) == 1
    assert brain_trace.steward_calls_mentioning("concern_handoff:other", connect=connect) == []


def test_a_failed_write_is_logged_not_raised(monkeypatch):
    logged = []
    monkeypatch.setattr(brain_trace.logger, "error", lambda msg, *args, **kw: logged.append(msg % args))
    def broken(write):
        raise OSError("disk full")
    _record(broken, result={})
    assert logged == ["[brain_trace] could not record a subconscious::gate call"]


class _Store:
    def execution_revoked(self, owner):
        return False


def test_dayflow_agents_and_calls_inside_work_are_recorded_and_capped(connect, monkeypatch):
    from app.assistant.manager_runtime.execution import REGISTRY, Owner
    monkeypatch.setattr(brain_trace, "KEEP_PER_AGENT", 3)
    _record(connect, agent="emi_team::planner", result={})                    # chat: not dayflow's
    _record(connect, agent="dayflow_orchestrator::switchboard", result={})
    with REGISTRY.span("tool", "work_emi_team_manager", owner=Owner(_Store(), "work_a", "task_1", 1)):
        _record(connect, agent="emi_team::planner", result={})
    calls = brain_trace.list_calls(connect=connect)
    assert sorted(c["agent"] for c in calls) == ["dayflow_orchestrator::switchboard", "emi_team::planner"]
    [inside] = brain_trace.work_attempt_calls(connect=connect)
    assert inside["trace"] == {"work_id": "work_a", "work_node": "task_1"}
    for _ in range(5):
        _record(connect, agent="dayflow_orchestrator::switchboard", result={})
        _record(connect, agent="subconscious::gate", result={})
    assert len(brain_trace.list_calls(agent="dayflow_orchestrator::switchboard", connect=connect)) == 3
    assert len(brain_trace.list_calls(agent="subconscious::gate", connect=connect)) == 5   # the brain keeps all


def test_the_dayflow_diagram_follows_the_managers_configs():
    from app.routes import dayflow_flow
    planning, wake, dispatch = (dayflow_flow._stages(m) for m, _, _ in dayflow_flow.MANAGERS)
    names = [s["name"] for s in planning]
    assert names[0] == "intake_triage_prep_node" and names[-1] == "post_room_finalize_node"
    assert names.index("dayflow_orchestrator::strategic_planner_wo") < names.index("work_architect_node") \
        < names.index("dayflow_orchestrator::switchboard") < names.index("work_node_dispatch_node")
    architect = next(s for s in planning if s["name"] == "work_architect_node")
    assert [a["name"] for a in architect["agents"]] == ["dayflow_orchestrator::work_architect"]
    assert [s["name"] for s in dispatch] == ["dayflow_switchboard_arguments_node", "dayflow_tool_caller",
                                             "work_finalizer_node"]
    assert next(s for s in dispatch if s["name"] == "dayflow_tool_caller")["work_calls"]
    assert "dayflow_orchestrator::state_mover" in [s["name"] for s in wake]
