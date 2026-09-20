from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from app.assistant.dayflow_orchestrator import work_session as sessions

@pytest.fixture
def claimed():
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Session"})
    store.apply("add_node", {"work_id": wo.id, "id": "task", "type": "subtask", "parent_id": wo.goal_node_id, "status": "actionable"})
    store.apply("claim_task", {"work_id": wo.id, "node_id": "task"})
    yield store, wo.id
    sessions._live_sessions.pop(sessions.session_id_for(wo.id, "task"), None)
    store.close()


def test_failed_thread_start_removes_registration(claimed, monkeypatch):
    store, wid = claimed
    thread = Mock()
    thread.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(sessions.threading, "Thread", lambda **kwargs: thread)
    with pytest.raises(RuntimeError):
        sessions.open_session(store, wid, "task", "work_emi_team_manager", expected_epoch=1)
    assert sessions.session_id_for(wid, "task") not in sessions._live_sessions


def test_same_attempt_cannot_register_twice(claimed, monkeypatch):
    store, wid = claimed
    thread = Mock()
    monkeypatch.setattr(sessions.threading, "Thread", lambda **kwargs: thread)
    sessions.open_session(store, wid, "task", "work_emi_team_manager", expected_epoch=1)
    with pytest.raises(ValueError):
        sessions.open_session(store, wid, "task", "work_emi_team_manager", expected_epoch=1)
    thread.start.assert_called_once()
