"""Recover persisted results through the ordinary finalizer, never by rerunning tools."""
from types import SimpleNamespace
import pytest
from work_objects.store import WorkStore

@pytest.fixture
def pending(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_store, work_session
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Recover judgment"})
    store.apply("add_node", {"work_id": wo.id, "id": "task", "type": "subtask", "parent_id": wo.goal_node_id})
    store.apply("set_status", {"work_id": wo.id, "node_id": "task", "status": "dispatched"})
    store.apply("set_status", {"work_id": wo.id, "node_id": "task", "status": "done"})
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: store)
    monkeypatch.setattr(work_session, "session_alive", lambda *a: False)
    monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda *a: None)
    calls = []
    def judge(msg):
        calls.append(msg)
        return SimpleNamespace(data={"verdict": "achieved", "outcome": "The saved result achieves the task"})
    monkeypatch.setattr(DI, "agent_factory", SimpleNamespace(create_agent=lambda *a, **kw: SimpleNamespace(action_handler=judge)))
    yield store, wo.id, calls
    store.close()


def test_saved_result_is_judged_once_without_dispatch(pending):
    from app.assistant.dayflow_orchestrator.work_session import recover_pending_finalizations
    store, wid, calls = pending
    assert recover_pending_finalizations() == 1
    assert store.load(wid).nodes["task"].status == "closed"
    assert recover_pending_finalizations() == 0
    assert len(calls) == 1


def test_a_live_dispatch_keeps_its_own_finalizer(pending, monkeypatch):
    from app.assistant.dayflow_orchestrator import work_session
    store, wid, calls = pending
    monkeypatch.setattr(work_session, "session_alive", lambda *a: True)
    assert work_session.recover_pending_finalizations() == 0
    assert not calls


def test_legacy_judgment_is_not_counted_again(pending):
    from app.assistant.dayflow_orchestrator.work_session import recover_pending_finalizations
    store, wid, calls = pending
    store.apply("set_status", {"work_id": wid, "node_id": "task", "status": "failed",
        "finalizer": {"verdict": "unrecoverable", "outcome": "Already judged before epoch markers", "next_step": "ask_user"}})
    assert recover_pending_finalizations() == 0
    assert not calls


def test_swept_timeout_can_be_judged_while_old_thread_remains_alive(pending, monkeypatch):
    from app.assistant.dayflow_orchestrator import work_session
    store, wid, calls = pending
    original = store.load
    def timeout_snapshot(work_id):
        wo = original(work_id)
        wo.nodes["task"].payload["result_actor"] = "dispatch_sweeper"
        return wo
    monkeypatch.setattr(store, "load", timeout_snapshot)
    monkeypatch.setattr(work_session, "session_alive", lambda *a: True)
    assert work_session.recover_pending_finalizations() == 1
    assert len(calls) == 1
