"""Judgment, not execution status, counts a failed attempt."""
import pytest
from work_objects.store import WorkStore

@pytest.fixture
def task():
    s = WorkStore(":memory:")
    wo = s.apply("create_work_object", {"title": "judge"})
    s.apply("add_node", {"work_id": wo.id, "id": "main", "type": "subtask", "parent_id": wo.goal_node_id})
    s.apply("set_status", {"work_id": wo.id, "node_id": "main", "status": "dispatched"})
    yield s, wo.id, wo.goal_node_id
    s.close()

def judge(s, wid, verdict="retry", epoch=1):
    return s.apply("finalize_task", {"work_id": wid, "node_id": "main", "expected_dispatch_epoch": epoch,
        "finalizer": {"verdict": verdict, "outcome": "The task was assessed", "recommendation": "Use the other source",
                      "next_step": "retry" if verdict == "retry" else ""}}, actor="finalizer")

def test_execution_failure_is_not_a_judged_failure(task):
    s, wid, gid = task
    wo = s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
    assert wo.nodes["main"].payload.get("failure_count", 0) == 0
    assert wo.nodes[gid].payload.get("goal_unmet_attempts", 0) == 0

def test_judgment_counts_once_even_if_tool_already_failed(task):
    s, wid, gid = task
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
    wo = judge(s, wid)
    assert wo.nodes["main"].status == "proposed"
    assert wo.nodes["main"].payload["failure_count"] == 1
    assert wo.nodes[gid].payload["goal_unmet_attempts"] == 1
    before = s.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        judge(s, wid)
    assert s.load(wid).model_dump(mode="json") == before

def test_stale_judgment_cannot_fail_successor(task):
    s, wid, gid = task
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "done"})
    before = s.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        judge(s, wid, epoch=0)
    assert s.load(wid).model_dump(mode="json") == before

def test_successful_judgment_does_not_count_tool_error(task):
    s, wid, gid = task
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
    wo = judge(s, wid, "achieved")
    assert wo.nodes["main"].status == "closed"
    assert wo.nodes["main"].payload.get("failure_count", 0) == 0

def test_second_judged_failure_escalates_atomically(task):
    s, wid, gid = task
    for epoch in (1, 2):
        s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
        wo = judge(s, wid, epoch=epoch)
        if epoch == 1:
            s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "dispatched"})
    assert wo.nodes["main"].status == "failed"
    assert wo.nodes["main"].payload["finalizer"]["next_step"] == "ask_user"
    assert wo.nodes[gid].payload["goal_unmet_attempts"] == 2
