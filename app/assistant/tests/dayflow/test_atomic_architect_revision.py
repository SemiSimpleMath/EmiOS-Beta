"""An invalid architect revision must leave the whole previous graph intact."""
import pytest
from work_objects.store import WorkStore
from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag

@pytest.fixture
def graph():
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Revise atomically"})
    store.apply("add_node", {"work_id": wo.id, "id": "existing", "type": "subtask", "parent_id": wo.goal_node_id, "status": "actionable"})
    yield store, wo.id
    store.close()

@pytest.mark.parametrize("invalid", [
    {"node_id": "later", "title": "Wait", "wake_at": "not-a-date"},
    {"node_id": "later", "title": "Unknown dependency", "depends_on": ["does_not_exist"]},
])
def test_invalid_revision_leaves_no_partially_runnable_graph(graph, invalid):
    store, wid = graph
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        apply_architect_dag(store, wid, [{"node_id": "new", "title": "Do work"}, invalid])
    assert store.load(wid).model_dump(mode="json") == before


def test_refused_prune_aborts_revision_instead_of_publishing_the_rest(graph):
    store, wid = graph
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        apply_architect_dag(store, wid, [{"node_id": "new", "title": "Different work"}], abandon_node_ids=["existing"])
    assert store.load(wid).model_dump(mode="json") == before


def test_batch_failure_rolls_back_earlier_mutation_and_event(graph):
    store, wid = graph
    before = store.load(wid).model_dump(mode="json")
    events = len(store.events(wid))
    with pytest.raises(ValueError, match="unknown batch operation"):
        store.apply("batch", {"work_id": wid, "operations": [
            {"op": "set_status", "data": {"node_id": "existing", "status": "dispatched"}},
            {"op": "invalid", "data": {}},
        ]})
    assert store.load(wid).model_dump(mode="json") == before
    assert len(store.events(wid)) == events


def test_a_newer_finalizer_instruction_cannot_be_consumed_by_an_old_revision(graph):
    store, wid = graph
    old = {"verdict": "retry", "outcome": "old attempt", "next_step": "retry"}
    newer = {"verdict": "retry", "outcome": "new attempt", "next_step": "retry"}
    store.apply("set_status", {"work_id": wid, "node_id": "existing", "status": "actionable", "finalizer": newer})
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError, match="instruction changed"):
        store.apply("consume_finalizer_instruction", {"work_id": wid, "node_id": "existing", "expected_finalizer": old})
    assert store.load(wid).model_dump(mode="json") == before


def test_deduplication_preserves_dependency_obligations(graph):
    store, wid = graph
    gid = store.load(wid).goal_node_id
    for nid in ("duplicate", "prerequisite", "consumer"):
        store.apply("add_node", {"work_id": wid, "id": nid, "type": "subtask", "parent_id": gid})
    for src, dst in (("prerequisite", "duplicate"), ("duplicate", "consumer")):
        store.apply("add_edge", {"work_id": wid, "src": src, "dst": dst, "relation": "depends_on"})
    apply_architect_dag(store, wid, [], duplicate_of={"duplicate": "existing"})
    wo = store.load(wid)
    assert wo.nodes["duplicate"].status == "abandoned"
    assert wo.deps_of("consumer") == ["existing"]
    assert wo.deps_of("existing") == ["prerequisite"]


def test_revision_and_instruction_are_committed_together(graph):
    store, wid = graph
    store.apply("set_status", {"work_id": wid, "node_id": "existing", "status": "actionable",
        "finalizer": {"verdict": "retry", "next_step": "retry", "outcome": "Use another route"}})
    before = store.load(wid)
    entry = {**before.nodes["existing"].payload["finalizer"], "node_id": "existing"}
    apply_architect_dag(store, wid, [{"node_id": "new", "title": "Another route"}],
                       finalizer_instructions=[entry], expected_updated_at=before.updated_at)
    after = store.load(wid)
    assert not after.has_pending_revision()
    assert any(n.title == "Another route" for n in after.nodes.values())
    assert store.events(wid)[-1]["op"] == "batch"


def test_stale_instruction_rolls_back_new_tasks_in_the_same_revision(graph):
    store, wid = graph
    store.apply("set_status", {"work_id": wid, "node_id": "existing", "status": "actionable",
        "finalizer": {"verdict": "retry", "next_step": "retry", "outcome": "Newer instruction"}})
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError, match="instruction changed"):
        apply_architect_dag(store, wid, [{"node_id": "new", "title": "Stale plan"}],
            finalizer_instructions=[{"node_id": "existing", "verdict": "retry", "next_step": "retry", "outcome": "Old instruction"}])
    assert store.load(wid).model_dump(mode="json") == before


def test_a_replan_restating_existing_dependencies_adds_only_the_new_ones(graph):
    """2026-10-05, work_062b58b07e65: a re-plan restated an existing task with its existing
    dependency; the store refused the duplicate edge and the whole re-plan was lost. The prompt
    tells the architect to reuse existing node_ids in depends_on, so a restated edge is the graph
    it already has, as a restated node is."""
    store, wid = graph
    gid = store.load(wid).goal_node_id
    store.apply("add_node", {"work_id": wid, "id": "ask", "type": "subtask", "parent_id": gid})
    store.apply("add_edge", {"work_id": wid, "src": "existing", "dst": "ask", "relation": "depends_on"})
    res = apply_architect_dag(store, wid, [
        {"node_id": "ask", "title": "Ask", "depends_on": ["existing", "existing"]},     # restated, and twice
        {"node_id": "act", "title": "Act on the answer", "depends_on": ["ask", "existing"]},
    ])
    wo = store.load(wid)
    assert res["added"] == ["act--" + wid.split("_")[-1][:6]] and res["edges"] == 2
    assert wo.deps_of("ask") == ["existing"]
    assert sorted(wo.deps_of(res["added"][0])) == ["ask", "existing"]


def test_a_restated_task_in_motion_keeps_its_wake_and_a_queued_one_takes_the_new_one(graph):
    """An in-flight ask restated with a wake_ref used to become an event wake and be parked (dispatched ->
    waiting), losing its user_reply wake. In-motion nodes are the runtime's."""
    store, wid = graph
    gid = store.load(wid).goal_node_id
    store.apply("add_node", {"work_id": wid, "id": "ask", "type": "subtask", "parent_id": gid, "status": "dispatched"})
    store.apply("defer_node", {"work_id": wid, "node_id": "ask", "wake_kind": "user_reply", "wake_ref": "the answer"})
    res = apply_architect_dag(store, wid, [
        {"node_id": "ask", "title": "Ask", "wake_ref": "the user's answer to the question"},
        {"node_id": "existing", "title": "Retry later", "wake_at": "2030-01-01T09:00:00+00:00"},
    ])
    wo = store.load(wid)
    assert (wo.nodes["ask"].status, wo.nodes["ask"].wake_kind) == ("dispatched", "user_reply")
    assert wo.nodes["existing"].wake_kind == "time" and res["waits"] == 1


def test_a_refused_revision_gets_one_correction_and_a_second_refusal_raises():
    from types import SimpleNamespace
    from app.assistant.control_nodes.work_architect_node import _apply_with_correction
    tasks = []
    answers = iter([{"nodes": ["fixed"]}, {"nodes": ["still bad"]}])
    agent = SimpleNamespace(action_handler=lambda m: tasks.append(m.task) or SimpleNamespace(data=next(answers)))

    def apply(d):
        if d["nodes"] != ["fixed"]:
            raise ValueError("add_edge: dependency cycle")
        return {"added": ["fixed"]}
    data, res = _apply_with_correction(agent, {"nodes": ["cyclic"]}, "TASK", "info", None, apply)
    assert res == {"added": ["fixed"]} and len(tasks) == 1
    assert tasks[0].startswith("TASK") and "WAS REFUSED" in tasks[0] and "dependency cycle" in tasks[0]
    with pytest.raises(ValueError, match="cycle"):
        _apply_with_correction(agent, {"nodes": ["cyclic"]}, "TASK", "info", None, apply)
