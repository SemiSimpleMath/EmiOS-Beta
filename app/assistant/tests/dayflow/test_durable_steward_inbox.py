"""Admission survives ticks; goal success criteria survive persistence."""
from datetime import datetime, timezone
from app.assistant.tests.dayflow.conftest import FakeBlackboard, make_dayflow_message, seed_items
from app.assistant.control_nodes.triage_persist_node import TriagePersistNode
from app.assistant.dayflow_orchestrator import state_store
from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output
from work_objects.store import WorkStore


def test_admitted_intake_can_be_reloaded_after_tick_blackboard_is_gone():
    item = make_dayflow_message(item_id="durable-request", short_id=123, state="artifact", summary="Handle this", source_type="delegation")
    seed_items([item])
    admitted = item.model_dump(mode="json")
    node = TriagePersistNode(name="persist", blackboard=FakeBlackboard({"admitted_artifacts": [admitted]}), agent_registry={}, tool_registry={})
    node._persist_admitted(datetime.now(timezone.utc))
    loaded = state_store.load_admitted_intake()
    assert [i["metadata"]["item_id"] for i in loaded] == ["durable-request"]


def test_steward_success_criteria_are_durable():
    with_store = WorkStore(":memory:")
    try:
        result = persist_steward_output(with_store, {"new_or_changed": [{"objective": "Prepare release", "success_criteria": "Existing users retain their data"}]})
        wo = with_store.load(result["created"][0]["work_id"])
        assert wo.constraints["success_criteria"] == "Existing users retain their data"
    finally:
        with_store.close()


def test_created_goal_has_source_detail_in_its_first_commit():
    store = WorkStore(":memory:")
    original = store.apply
    snapshots = []
    def apply(op, data, **kwargs):
        result = original(op, data, **kwargs)
        if op == "create_work_object":
            snapshots.append(result)
        return result
    store.apply = apply
    try:
        persist_steward_output(store, {"new_or_changed": [{"objective": "Handle flight", "based_on": ["700"]}]}, admitted_artifacts=[{"metadata": {"item_id": "email:flight", "short_id": "700", "summary": "Flight changed to 6:40", "pod_id": "datapod:flight"}}])
        goal = snapshots[0].nodes[snapshots[0].goal_node_id]
        assert "Flight changed to 6:40" in goal.content
        assert "datapod:flight" in goal.content
    finally:
        store.close()


def test_changed_goal_preserves_sources_and_updates_success_criteria():
    store = WorkStore(":memory:")
    try:
        first = persist_steward_output(store, {"new_or_changed": [{"objective": "Original", "based_on": ["1"]}]}, admitted_artifacts=[{"metadata": {"item_id": "request:1", "short_id": "1", "summary": "Retain this source"}}])
        wid = first["created"][0]["work_id"]
        persist_steward_output(store, {"new_or_changed": [{"work_id": wid, "objective": "Revised", "success_criteria": "Accepted by user"}]})
        wo = store.load(wid)
        assert "Retain this source" in wo.nodes[wo.goal_node_id].content
        assert wo.constraints["success_criteria"] == "Accepted by user"
    finally:
        store.close()


def test_fold_failure_cannot_close_source(monkeypatch):
    from app.assistant.control_nodes.strategic_planner_wo_persist_node import StrategicPlannerWoPersistNode
    from app.assistant.dayflow_orchestrator import work_store, dayflow_item_writer
    from types import SimpleNamespace
    from unittest.mock import Mock
    import pytest
    node = StrategicPlannerWoPersistNode(name="persist", blackboard=FakeBlackboard({"admitted_artifacts": [{"metadata": {"item_id": "source", "summary": "Must survive"}}]}), agent_registry={}, tool_registry={})
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: SimpleNamespace(load=Mock(side_effect=OSError("store unavailable"))))
    close = Mock()
    monkeypatch.setattr(dayflow_item_writer, "write_dayflow_item", close)
    with pytest.raises(OSError):
        node._close_consumed_items([{"work_id": "work", "based_on": ["source"]}])
    close.assert_not_called()


def test_restarted_tick_repairs_source_ack_after_goal_was_saved(monkeypatch):
    from unittest.mock import Mock
    from app.assistant.dayflow_orchestrator import work_intake, dayflow_item_writer
    store = WorkStore(":memory:")
    item = {"metadata": {"item_id": "source", "summary": "Do this"}}
    try:
        created = persist_steward_output(store, {"new_or_changed": [{"objective": "Do this", "based_on": ["source"]}]}, admitted_artifacts=[item])
        close = Mock()
        monkeypatch.setattr(dayflow_item_writer, "write_dayflow_item", close)
        assert work_intake.reconcile_transferred_intake(store, [item]) == []
        assert close.call_args.args == ("source",)
        assert created["created"][0]["work_id"] in close.call_args.kwargs["reason"]
    finally:
        store.close()


def test_undecomposed_goals_survive_lost_tick_blackboard():
    from app.assistant.control_nodes.work_architect_node import _undecomposed_goals
    store = WorkStore(":memory:")
    try:
        wo = store.apply("create_work_object", {"title": "Persisted before crash", "goal_content": "Recover this objective"})
        found = _undecomposed_goals(store, [])
        assert found[0]["work_id"] == wo.id
        assert found[0]["objective"] == "Recover this objective"
    finally:
        store.close()
