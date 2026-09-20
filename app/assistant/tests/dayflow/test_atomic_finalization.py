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


def test_success_resets_escalation_episode_without_erasing_history(task):
    s, wid, gid = task
    s.apply("add_node", {"work_id": wid, "id": "later", "type": "subtask", "parent_id": gid})
    for epoch in (1, 2):
        s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
        judge(s, wid, epoch=epoch)
        if epoch == 1:
            s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "dispatched"})
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "dispatched"})
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "done"})
    wo = judge(s, wid, "achieved_plan_changes", epoch=3)
    assert wo.nodes[gid].payload["goal_unmet_attempts"] == 2
    assert wo.nodes[gid].payload["goal_unmet_since_progress"] == 0
    s.apply("set_status", {"work_id": wid, "node_id": "later", "status": "dispatched"})
    s.apply("set_status", {"work_id": wid, "node_id": "later", "status": "failed"})
    wo = s.apply("finalize_task", {"work_id": wid, "node_id": "later", "expected_dispatch_epoch": 1,
        "finalizer": {"verdict": "retry", "outcome": "Temporary failure", "next_step": "retry"}})
    assert wo.nodes["later"].payload["finalizer"]["escalated"] is False
    assert wo.nodes[gid].payload["goal_unmet_attempts"] == 3


def test_stop_is_never_overridden_by_repeat_failure(task):
    s, wid, gid = task
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
    judge(s, wid)
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "dispatched"})
    s.apply("set_status", {"work_id": wid, "node_id": "main", "status": "failed"})
    wo = s.apply("finalize_task", {"work_id": wid, "node_id": "main", "expected_dispatch_epoch": 2,
        "finalizer": {"verdict": "unrecoverable", "outcome": "User stopped the task", "next_step": "stop"}})
    assert wo.nodes["main"].payload["finalizer"]["next_step"] == "stop"
    assert not wo.nodes["main"].payload["finalizer"]["escalated"]


def test_portfolio_history_does_not_reissue_consumed_question(task):
    from app.assistant.dayflow_orchestrator.work_context import render_view, work_data
    s, wid, gid = task
    wo = s.load(wid)
    wo.nodes[gid].payload["goal_unmet_attempts"] = 5
    wo.nodes["main"].payload["finalizer"] = {"verdict": "unrecoverable",
        "next_step": "ask_user", "question_for_user": "Continue?", "consumed_at": "2026-09-19"}
    rendered = render_view("portfolio", work=work_data(wo))
    assert "goal failures judged by finalizer: 5" in rendered
    assert "PRIOR QUESTION" in rendered
    assert "ASK THE USER:" not in rendered
    assert "ATTEMPTS HAVE NOT ACHIEVED" not in rendered
