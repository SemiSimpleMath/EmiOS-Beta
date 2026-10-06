"""The steward's retirement switch (configs/brain_lanes.json steward.mode; owner, 2026-10-05: all input flows to the
brain, the brain decides, consumers carry it out).

Pins: retired, the prep node keeps the work others read every tick (the portfolio and reminder history the architect
reads, pending feedback delivered), skips the steward's own context, hands the architect an empty steward result and
routes straight to the list-item intake node; on, it leaves routing to the state map as before; an unknown mode raises.
"""
import pytest

from app.assistant.control_nodes import strategic_planner_wo_prep_node as steward
from app.assistant.dayflow_orchestrator import blackboard_builder
from app.assistant.dayflow_orchestrator import brain_list_intake as B
from app.assistant.dayflow_orchestrator import communication_context as context
from app.assistant.tests.dayflow.conftest import FakeBlackboard

LANES = {"calendar": {"mode": "live", "from_day": "2026-10-06"}, "concern": {"mode": "live", "since": "x"}}


def _run(monkeypatch, mode):
    monkeypatch.setattr(B, "lanes", lambda: {**LANES, "steward": {"mode": mode}})
    monkeypatch.setattr(context, "scheduled_reminder_history", lambda now: {"status": "available", "records": []})
    monkeypatch.setattr(blackboard_builder, "build_dayflow_blackboard_extras", lambda: {})
    built = []
    monkeypatch.setattr(steward.StrategicPlannerWoPrepNode, "_build_situational_context",
                        lambda self: built.append(True))
    bb = FakeBlackboard({"replan_work_ids": ["stale"]})
    steward.StrategicPlannerWoPrepNode(name="strategic_planner_wo_prep_node", blackboard=bb, agent_registry={},
                                       tool_registry={}).action_handler(None)
    return bb, built


def test_retired_the_tick_goes_straight_to_the_list_intake_with_the_shared_context(monkeypatch):
    bb, built = _run(monkeypatch, "retired")
    assert bb.get_state_value("next_agent") == "brain_list_intake_node"
    assert bb.get_state_value("steward_persist_result") == {"created": [], "changed": [], "end_requested": []}
    assert bb.get_state_value("replan_work_ids") == []
    assert bb.get_state_value("work_portfolio") is not None and bb.get_state_value("scheduled_reminder_history")
    assert built == [], "the steward's own context is not built"


def test_on_the_state_map_routes_to_the_steward_as_before(monkeypatch):
    bb, built = _run(monkeypatch, "on")
    assert bb.get_state_value("next_agent") is None and built == [True]


def test_an_unknown_steward_mode_raises(monkeypatch, tmp_path):
    import json
    from app.assistant.utils import path_utils
    (tmp_path / "brain_lanes.json").write_text(json.dumps({**LANES, "steward": {"mode": "off"}}), encoding="utf-8")
    monkeypatch.setattr(path_utils, "get_configs_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="steward.mode"):
        B.lanes()
