"""New graph mutations must preserve runnable DAG and judgment contracts."""
import pytest
from work_objects.store import WorkStore

@pytest.fixture
def graph():
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Goal"})
    for nid in ("a", "b"):
        store.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask", "parent_id": wo.goal_node_id})
    yield store, wo.id, wo.goal_node_id
    store.close()

@pytest.mark.parametrize("src,dst", [("a", "a"), ("a", "b"), ("b", "a")])
def test_reject_self_duplicate_and_cyclic_dependencies(graph, src, dst):
    store, wid, _ = graph
    store.apply("add_edge", {"work_id": wid, "src": "a", "dst": "b", "relation": "depends_on"})
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        store.apply("add_edge", {"work_id": wid, "src": src, "dst": dst, "relation": "depends_on"})
    assert store.load(wid).model_dump(mode="json") == before


def test_initial_status_must_belong_to_node_family(graph):
    store, wid, gid = graph
    with pytest.raises(ValueError):
        store.apply("add_node", {"work_id": wid, "type": "subtask", "parent_id": gid, "status": "banana"})


def test_authority_ceiling_survives_unspecified_intermediate_parent(graph):
    store, wid, gid = graph
    for nid, parent, auth in (("ceiling", gid, 20), ("middle", "ceiling", None)):
        store.apply("add_node", {"work_id": wid, "id": nid, "type": "subtask", "parent_id": parent, "authority": auth})
    with pytest.raises(ValueError, match="authority"):
        store.apply("add_node", {"work_id": wid, "type": "subtask", "parent_id": "middle", "authority": 99})


def test_main_task_cannot_complete_through_helper_children(graph):
    store, wid, gid = graph
    wo = store.load(wid)
    task = wo.nodes["a"]
    task.satisfied_when_kind = "all_owned_children_done"
    from work_objects.model import WorkNode
    wo.add_node(WorkNode(id="helper", work_id=wid, type="subtask", parent_id="a", status="done"))
    assert not wo.is_satisfied(task)
    task.status = "closed"
    assert wo.is_satisfied(task)


def test_automatic_completion_marks_goal_done_even_if_not_dispatched(graph):
    store, wid, gid = graph
    for nid in ("a", "b"):
        for status in ("actionable", "dispatched", "done", "closed"):
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": status, "reason": "verified"})
    wo = store.load(wid)
    assert wo.status == "done"
    assert wo.nodes[gid].status == "done"


@pytest.mark.parametrize("kind", ["time", "event", "signal"])
def test_defer_requires_a_usable_wake_condition(graph, kind):
    store, wid, _ = graph
    with pytest.raises(ValueError):
        store.apply("defer_node", {"work_id": wid, "node_id": "a", "wake_kind": kind})


def test_external_wake_is_not_ready_until_matched(graph):
    store, wid, _ = graph
    store.apply("defer_node", {"work_id": wid, "node_id": "a", "wake_kind": "event", "wake_ref": "user sends revised dates"})
    wo = store.load(wid)
    assert not wo.is_ready(wo.nodes["a"])
