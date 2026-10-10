"""A standing watch: a task waiting on an outside event runs each time it happens, until a time (owner, 2026-10-10:
"Success but keep node open if the node is of the type where it needs to be ran repeatedly", event waits only)."""
from datetime import datetime, timedelta, timezone

import pytest

import work_objects.store as store_module
from work_objects.store import WorkStore

UNTIL = datetime.now(timezone.utc) + timedelta(hours=3)


@pytest.fixture
def watch():
    s = WorkStore(":memory:")
    wo = s.apply("create_work_object", {"title": "forward each further email"})
    s.apply("batch", {"work_id": wo.id, "operations": [
        {"op": "add_node", "data": {"id": "fwd", "type": "subtask", "parent_id": wo.goal_node_id}},
        {"op": "defer_node", "data": {"node_id": "fwd", "wake_kind": "event", "wake_ref": "a further email from X"}},
        {"op": "watch_node", "data": {"node_id": "fwd", "until": UNTIL}}]})
    yield s, wo.id
    s.close()


def run(s, wid, epoch, verdict="achieved_keep_open"):
    """One occurrence: woken (the wake clears the task's own condition), run, judged."""
    s.apply("defer_node", {"work_id": wid, "node_id": "fwd", "wake_kind": None})
    s.apply("set_status", {"work_id": wid, "node_id": "fwd", "status": "dispatched"})
    s.apply("set_status", {"work_id": wid, "node_id": "fwd", "status": "done"})
    return s.apply("finalize_task", {"work_id": wid, "node_id": "fwd", "expected_dispatch_epoch": epoch,
                                     "finalizer": {"verdict": verdict, "outcome": f"forwarded email {epoch}"}},
                   actor="finalizer")


def later(monkeypatch, delta):
    monkeypatch.setattr(store_module, "utcnow", lambda: datetime.now(timezone.utc) + delta)


def test_a_run_achieved_waits_again_on_the_same_condition(watch):
    s, wid = watch
    wo = run(s, wid, 1)
    node = wo.nodes["fwd"]
    assert (node.status, node.wake_kind, node.wake_ref) == ("waiting", "event", "a further email from X")
    assert wo.status == "active"
    assert [r["outcome"] for r in node.payload["watch"]["runs"]] == ["forwarded email 1"]
    assert node.payload["watch"]["waiting_since"] == node.payload["finalizer"]["at"]
    assert not wo.needs_finalization(node) and not wo.has_pending_revision()
    wo = run(s, wid, 2)
    assert wo.nodes["fwd"].status == "waiting"
    assert [r["attempt"] for r in wo.nodes["fwd"].payload["watch"]["runs"]] == [1, 2]


def test_keep_open_is_only_for_a_watch():
    s = WorkStore(":memory:")
    wo = s.apply("create_work_object", {"title": "once"})
    s.apply("add_node", {"work_id": wo.id, "id": "fwd", "type": "subtask", "parent_id": wo.goal_node_id})
    s.apply("set_status", {"work_id": wo.id, "node_id": "fwd", "status": "dispatched"})
    s.apply("set_status", {"work_id": wo.id, "node_id": "fwd", "status": "done"})
    with pytest.raises(ValueError, match="not a standing watch"):
        s.apply("finalize_task", {"work_id": wo.id, "node_id": "fwd", "expected_dispatch_epoch": 1,
                                  "finalizer": {"verdict": "achieved_keep_open", "outcome": "sent"}})


def test_only_an_event_wait_becomes_a_watch():
    s = WorkStore(":memory:")
    wo = s.apply("create_work_object", {"title": "hourly"})
    s.apply("add_node", {"work_id": wo.id, "id": "t", "type": "subtask", "parent_id": wo.goal_node_id,
                         "wake_kind": "time", "wake_at": UNTIL})
    with pytest.raises(ValueError, match="outside event"):
        s.apply("watch_node", {"work_id": wo.id, "node_id": "t", "until": UNTIL})


def test_achieved_closes_the_watch_and_completes_the_goal(watch):
    s, wid = watch
    wo = run(s, wid, 1, verdict="achieved")
    assert wo.nodes["fwd"].status == "closed" and wo.status == "done"


def test_a_run_past_the_end_closes_it(watch, monkeypatch):
    s, wid = watch
    later(monkeypatch, timedelta(hours=4))
    wo = run(s, wid, 1)
    assert wo.nodes["fwd"].status == "closed" and wo.status == "done"


def test_the_end_closes_a_waiting_watch(watch, monkeypatch):
    s, wid = watch
    run(s, wid, 1)
    with pytest.raises(ValueError, match="runs until"):
        s.apply("end_watch", {"work_id": wid, "node_id": "fwd"})
    later(monkeypatch, timedelta(hours=4))
    from app.assistant.dayflow_orchestrator.watches import end_watches, watch_end
    assert watch_end(s.load(wid)).isoformat() == UNTIL.isoformat()
    assert end_watches(s, datetime.now(timezone.utc) + timedelta(hours=4)) == [f"{wid}::fwd"]
    wo = s.load(wid)
    node = wo.nodes["fwd"]
    assert (node.status, node.wake_kind, node.payload["terminal"]["verdict"]) == ("closed", None, "watch_ended")
    assert wo.status == "done"


def test_a_woken_watch_is_left_to_run(watch, monkeypatch):
    s, wid = watch
    s.apply("defer_node", {"work_id": wid, "node_id": "fwd", "wake_kind": None})
    later(monkeypatch, timedelta(hours=4))
    from app.assistant.dayflow_orchestrator.watches import end_watches
    assert end_watches(s, datetime.now(timezone.utc) + timedelta(hours=4)) == []


def test_the_architect_marks_a_watch():
    from app.assistant.agents.dayflow_orchestrator.work_architect.agent_form import WorkNode
    from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
    with pytest.raises(ValueError, match="repeats_until"):
        WorkNode(node_id="x", title="x", detail="x", wake_at=UNTIL.isoformat(), repeats_until=UNTIL.isoformat())
    s = WorkStore(":memory:")
    wo = s.apply("create_work_object", {"title": "watch"})
    apply_architect_dag(s, wo.id, [{"node_id": "fwd", "title": "Forward each further email", "detail": "d",
                                    "wake_ref": "a further email from X", "repeats_until": UNTIL.isoformat()}])
    [node] = [n for n in s.load(wo.id).nodes.values() if n.id.startswith("fwd")]
    assert node.payload["watch"]["condition"] == "a further email from X"
    assert datetime.fromisoformat(node.payload["watch"]["until"]) == UNTIL


def test_the_finalizer_form_takes_keep_open_without_a_recommendation():
    from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm
    assert AgentForm(outcome="forwarded", verdict="achieved_keep_open").verdict == "achieved_keep_open"
