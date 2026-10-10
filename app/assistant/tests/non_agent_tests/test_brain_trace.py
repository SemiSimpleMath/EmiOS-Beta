"""Recording the brain's model calls for the /brain page.

Pins: a traced agent's call is stored with the system and user prompts as sent, the result, the
engine and the tags of the block it ran in (tags nest and unwind); an error is stored instead of a
result; untraced agents are not stored; calls are found by event and by concern; a failed write is
logged, never raised into the call. A wake is a run: its calls carry its id, the decisions code made
are its steps (none recorded outside a run), a run that raises keeps its error; runs list newest first,
idle ones only on request, and by concern. Scratch store; invented data only.
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


def test_calls_are_found_by_concern(connect):
    with brain_trace.trace(stage="summary", concern_id="c-bake"):
        _record(connect, agent="subconscious::concern_summary", result={"what": "w"})
    _record(connect, agent="subconscious::concern_summary", result={"what": "w"})        # no concern: not its call
    assert len(brain_trace.calls_for_concern("c-bake", connect=connect)) == 1


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


def test_replay_agents_are_recorded_capped_and_kept_off_the_brain_list(connect, monkeypatch):
    monkeypatch.setattr(brain_trace, "KEEP_PER_AGENT", 2)
    for _ in range(4):
        _record(connect, agent="label", result={"label": "x"})
    _record(connect, result={"decisions": []})
    assert len(brain_trace.list_calls(agent="label", connect=connect)) == 2
    assert [c["agent"] for c in brain_trace.list_calls(agents=brain_trace.TRACED_AGENTS, connect=connect)] == [
        "subconscious::gate"]


def test_the_dayflow_diagram_follows_the_managers_configs():
    from app.routes import dayflow_flow
    planning, wake, dispatch = (dayflow_flow._stages(m) for m, _, _ in dayflow_flow.MANAGERS)
    names = [s["name"] for s in planning]
    assert names[0] == "work_prep_node" and names[-1] == "post_room_finalize_node"
    assert names.index("brain_list_intake_node") < names.index("work_architect_node") \
        < names.index("dayflow_orchestrator::switchboard") < names.index("work_node_dispatch_node")
    architect = next(s for s in planning if s["name"] == "work_architect_node")
    assert [a["name"] for a in architect["agents"]] == ["dayflow_orchestrator::work_architect"]
    assert [s["name"] for s in dispatch] == ["dayflow_switchboard_arguments_node", "dayflow_tool_caller",
                                             "work_finalizer_node"]
    assert next(s for s in dispatch if s["name"] == "dayflow_tool_caller")["work_calls"]
    assert "dayflow_orchestrator::state_mover" in [s["name"] for s in wake]


def test_a_run_holds_its_calls_and_steps(connect):
    brain_trace.step("brief", "stale", subject="c-1", connect=connect)          # outside a run: not recorded
    with brain_trace.run("event", connect=connect) as recorded:
        with brain_trace.trace(stage="brief", concern_id="c-1"):
            _record(connect, agent="subconscious::concern_summary", result={"what": "w"})
            brain_trace.step("brief", "stale", subject="c-1", reason="no brief yet", connect=connect)
        brain_trace.step("handoff", "handed", subject="c-2", item_id="concern_handoff:c-2:abc", connect=connect)
        recorded["summary"] = {"summaries_written": 1}
    with brain_trace.run("timer", connect=connect) as recorded:                  # idle
        recorded["summary"] = {}
    [run] = brain_trace.list_runs(connect=connect)
    assert run["trigger"] == "event" and run["summary"] == {"summaries_written": 1} and run["ended_at"]
    assert (run["steps"], run["calls"]) == (2, 1)
    assert len(brain_trace.list_runs(active_only=False, connect=connect)) == 2
    assert [r["id"] for r in brain_trace.list_runs(concern_id="c-2", connect=connect)] == [run["id"]]
    assert brain_trace.list_runs(concern_id="c-9", connect=connect) == []
    full = brain_trace.get_run(run["id"], connect=connect)
    assert [(s["stage"], s["what"], s["subject"]) for s in full["steps"]] == [
        ("brief", "stale", "c-1"), ("handoff", "handed", "c-2")]
    assert full["steps"][0]["detail"] == {"reason": "no brief yet"}
    [call] = full["calls"]
    assert call["trace"] == {"run_id": run["id"], "stage": "brief", "concern_id": "c-1"}


def test_a_run_that_raises_keeps_its_error(connect):
    with pytest.raises(RuntimeError):
        with brain_trace.run("boot", connect=connect):
            raise RuntimeError("register locked")
    [run] = brain_trace.list_runs(connect=connect)
    assert run["error"] == "RuntimeError: register locked" and run["summary"] is None
    assert brain_trace.current("run_id") is None


def test_the_brain_diagram_is_consistent():
    from app.routes import brain_flow
    ids = {n["id"] for n in brain_flow.NODES}
    assert all(a in ids and b in ids for a, b, _ in brain_flow.EDGES)
    for agent in brain_flow.AGENT_NODE:
        assert brain_flow._engine(agent), f"{agent} has no config with a model tier"
    brain_agents = {a for a in brain_trace.TRACED_AGENTS if a != "subconscious::noticer"}
    assert brain_agents <= set(brain_flow.AGENT_NODE), "every traced brain agent has a place in the diagram"
