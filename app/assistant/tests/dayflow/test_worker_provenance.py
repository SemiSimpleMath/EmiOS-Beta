"""Worker provenance is durable context, never independently scheduled work."""
from types import SimpleNamespace
import pytest
from work_objects.store import WorkStore
from app.assistant.tests.dayflow.conftest import FakeBlackboard

@pytest.fixture
def graph(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_store
    store = WorkStore(":memory:")
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: store)
    wo = store.apply("create_work_object", {"title": "Deliver assessment"})
    for nid, parent, status, content in [
        ("main", wo.goal_node_id, "dispatched", "Check both accounts before reporting"),
        ("next", wo.goal_node_id, "proposed", "Deliver the final assessment"),
        ("helper", "main", "proposed", "Tried account A; access denied"),
        ("nested", "helper", "done", "Account B supplied the missing date"),
        ("queued_helper", "main", "actionable", "Internal pending investigation"),
    ]:
        store.apply("add_node", {"work_id": wo.id, "id": nid, "parent_id": parent,
                                 "type": "subtask", "title": nid, "content": content,
                                 "status": status})
    store.apply("add_node", {"work_id": wo.id, "id": "receipt", "parent_id": "nested",
                             "type": "evidence", "content": "Verified date: October 9"})
    yield store, wo.id
    store.close()

def test_tick_does_not_offer_or_promote_worker_provenance(graph):
    from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
    from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
    from app.assistant.control_nodes.work_node_materializer_node import WorkNodeMaterializerNode
    store, wid = graph
    bb = FakeBlackboard()
    StateMoverPrepNode(name="prep", blackboard=bb, agent_registry={}, tool_registry={})._build_promotion_candidates()
    assert [r["task_id"] for r in bb.get_state_value("ready_work_nodes")] == [f"{wid}::next"]
    StateMoverPersistNode(name="persist", blackboard=bb, agent_registry={}, tool_registry={})._promote_ready_nodes()
    wo = store.load(wid)
    assert wo.nodes["helper"].status == "proposed"
    assert [r["item_id"] for r in WorkNodeMaterializerNode._node_items(wo, None)] == [f"{wid}::next"]

def test_dispatch_rejects_helper_without_mutating_it(graph):
    from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
    store, wid = graph
    node = WorkNodeDispatchNode(name="dispatch", blackboard=FakeBlackboard(), agent_registry={}, tool_registry={})
    before = store.load(wid).nodes["queued_helper"].model_dump()
    with pytest.raises(ValueError, match="provenance"):
        node._claim(store, wid, "queued_helper")
    node._fail_node(wid, "queued_helper")
    assert store.load(wid).nodes["queued_helper"].model_dump() == before

def test_takeover_worker_and_finalizer_receive_full_provenance(graph, monkeypatch):
    from app.assistant.control_nodes.workobject_render_node import render_work_projection
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    from app.assistant.ServiceLocator.service_locator import DI
    store, wid = graph
    wo = store.load(wid)
    captured = []
    agent = SimpleNamespace(action_handler=lambda msg: captured.append(msg) or SimpleNamespace(data={}))
    monkeypatch.setattr(DI, "agent_factory", SimpleNamespace(create_agent=lambda *a, **k: agent))
    judge = WorkFinalizerNode(name="finalizer", blackboard=FakeBlackboard(), agent_registry={}, tool_registry={})
    judge._judge(wo, wo.nodes["main"], None)
    for text in [render_work_projection(wo, "main"), captured[0].information]:
        assert "PROVENANCE" in text
        assert "Tried account A; access denied" in text
        assert "Account B supplied the missing date" in text
        assert "Verified date: October 9" in text
        assert "Check both accounts before reporting" in text

def test_planners_receive_finalizer_summary_not_worker_history(graph):
    from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio
    from app.assistant.control_nodes.work_architect_node import _render_existing_graph
    store, wid = graph
    store.apply("set_status", {"work_id": wid, "node_id": "main", "status": "done",
        "finalizer": {"verdict": "achieved", "outcome": "Assessment complete; date confirmed"}})
    wo = store.load(wid)
    for text in [render_work_portfolio(wo), _render_existing_graph(wo)]:
        assert "Assessment complete; date confirmed" in text
        for hidden in ["helper", "access denied", "Verified date: October 9", "Account B supplied"]:
            assert hidden not in text

def test_helper_failure_does_not_count_as_main_goal_failure(graph):
    store, wid = graph
    for state in ["dispatched", "failed"]:
        store.apply("set_status", {"work_id": wid, "node_id": "helper", "status": state})
    wo = store.load(wid)
    assert wo.nodes[wo.goal_node_id].payload.get("goal_unmet_attempts", 0) == 0


def test_time_wakes_and_event_wakes_exclude_provenance(graph):
    from datetime import datetime, timedelta, timezone
    from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler
    from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
    from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
    store, wid = graph
    due = datetime.now(timezone.utc) + timedelta(minutes=5)
    for nid in ["next", "helper"]:
        store.apply("defer_node", {"work_id": wid, "node_id": nid, "wake_kind": "time", "wake_at": due})
    jobs = []
    timer = SimpleNamespace(add_job=lambda **kw: jobs.append(kw), get_job=lambda *a: None, get_jobs=lambda: [])
    scheduler = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=timer), app=None)
    scheduler._started = True
    scheduler._arm_work_node_wakes()
    assert [j["args"] for j in jobs] == [[wid, "next"]]
    store.apply("defer_node", {"work_id": wid, "node_id": "helper", "wake_kind": "event", "wake_ref": "reply"})
    bb = FakeBlackboard({"node_wakes": [{"task_id": f"{wid}::helper", "evidence": "arrived"}]})
    StateMoverPrepNode(name="prep", blackboard=bb, agent_registry={}, tool_registry={})._build_work_object_waits([])
    assert bb.get_state_value("waiting_work_nodes") == []
    StateMoverPersistNode(name="persist", blackboard=bb, agent_registry={}, tool_registry={})._apply_node_wakes()
    assert store.load(wid).nodes["helper"].wake_kind == "event"


def test_wake_callback_cannot_start_helper_manager(graph, monkeypatch):
    from contextlib import nullcontext
    from app.assistant.dayflow_orchestrator import dayflow_scheduler as mod
    from app.assistant.ServiceLocator.service_locator import DI
    calls = []
    monkeypatch.setattr(mod, "setup_complete", lambda: True)
    monkeypatch.setattr(DI, "multi_agent_manager_factory", SimpleNamespace(create_manager=lambda *a: calls.append(a)))
    store, wid = graph
    scheduler = mod.DayflowScheduler(timing_engine=SimpleNamespace(scheduler=None), app=SimpleNamespace(app_context=nullcontext))
    scheduler._fire_work_node(wid, "helper")
    assert calls == []


@pytest.mark.parametrize("delta", [
    {"abandon_node_ids": ["helper"], "licensed": True},
    {"nodes": [{"node_id": "new", "title": "new", "depends_on": ["helper"]}]},
    {"duplicate_of": {"helper": "next"}},
])
def test_architect_cannot_address_provenance(graph, delta):
    from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
    store, wid = graph
    before = store.load(wid).model_dump(mode="json")
    args = {"nodes": [], **delta}
    with pytest.raises(ValueError, match="provenance"):
        apply_architect_dag(store, wid, **args)
    assert store.load(wid).model_dump(mode="json") == before


def test_ui_labels_provenance_without_marking_it_ready(graph, monkeypatch):
    from flask import Flask
    from work_objects.ui import blueprint
    store, wid = graph
    monkeypatch.setattr(blueprint, "_get_store", lambda: store)
    app = Flask(__name__)
    app.register_blueprint(blueprint.work_ui_bp)
    data = app.test_client().get(f"/api/work/{wid}").get_json()
    records = {n["id"]: n for n in data["nodes"]}
    assert records["main"]["record_role"] == "task"
    assert records["queued_helper"]["record_role"] == "provenance"
    assert not records["queued_helper"]["ready"]
