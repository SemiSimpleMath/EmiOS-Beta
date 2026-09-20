"""Manager failure must produce an epoch-fenced result, then use the ordinary finalizer."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from app.assistant.utils.pydantic_classes import ToolResult
from app.assistant.tests.dayflow.conftest import FakeBlackboard

@pytest.mark.parametrize("crashes", [False, True])
def test_dispatch_manager_failure_records_evidence_and_calls_finalizer(monkeypatch, crashes):
    from app.assistant.dayflow_orchestrator import work_session
    from app.assistant.ServiceLocator.service_locator import DI
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Dispatch failure"})
    store.apply("add_node", {"work_id": wo.id, "id": "task", "type": "subtask", "parent_id": wo.goal_node_id})
    store.apply("set_status", {"work_id": wo.id, "node_id": "task", "status": "dispatched"})
    manager = SimpleNamespace(blackboard=FakeBlackboard())
    monkeypatch.setattr(DI, "multi_agent_manager_factory", SimpleNamespace(create_manager=lambda name: manager))
    def invoke(*args):
        if crashes:
            raise RuntimeError("synthetic dispatch crash")
        return ToolResult(result_type="error", content="synthetic dispatch abort", data={"aborted": True})
    monkeypatch.setattr(DI, "manager_invoker", SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(work_session, "room_session_scope", lambda *args: None)
    finalize = Mock()
    monkeypatch.setattr(work_session, "_finalize_recorded_result", finalize)
    work_session._run_dispatch_room(store, wo.id, "task", "synthetic-session", "test-tool", 1)
    saved = store.load(wo.id)
    assert saved.nodes["task"].status == "failed"
    evidence = [n.content for n in saved.provenance_for("task") if n.type == "evidence"]
    assert any("synthetic dispatch" in text for text in evidence)
    assert saved.nodes["task"].payload.get("failure_count", 0) == 0
    finalize.assert_called_once_with(wo.id, "task", expected_epoch=1)
    store.close()
