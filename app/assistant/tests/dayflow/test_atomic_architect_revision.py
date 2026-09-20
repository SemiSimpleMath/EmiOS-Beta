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
