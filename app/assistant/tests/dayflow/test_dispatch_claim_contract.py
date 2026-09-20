"""Dispatch must acquire exactly one current, ready main assignment."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from work_objects.store import WorkStore
from work_objects.model import utcnow
from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard

@pytest.fixture
def ready(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_store
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Claim contract"})
    for nid in ("task", "other"):
        store.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask", "parent_id": wo.goal_node_id, "status": "actionable"})
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: store)
    node = WorkNodeDispatchNode(name="claim", blackboard=FakeBlackboard(), agent_registry={}, tool_registry={})
    yield store, wo.id, node
    store.close()


def test_duplicate_claim_is_rejected_without_changing_owner(ready):
    store, wid, dispatch = ready
    dispatch._claim(store, wid, "task")
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        dispatch._claim(store, wid, "task")
    assert store.load(wid).model_dump(mode="json") == before

@pytest.mark.parametrize("gate", ["dependency", "future", "event", "revision"])
def test_claim_rechecks_current_gates(ready, gate):
    store, wid, dispatch = ready
    if gate == "dependency":
        store.apply("add_edge", {"work_id": wid, "src": "other", "dst": "task", "relation": "depends_on"})
    elif gate in {"future", "event"}:
        store.apply("defer_node", {"work_id": wid, "node_id": "task", "wake_kind": "time" if gate == "future" else "event",
            "wake_at": utcnow() + timedelta(hours=1) if gate == "future" else None,
            "wake_ref": "reply" if gate == "event" else None})
    else:
        store.apply("set_status", {"work_id": wid, "node_id": "other", "status": "actionable",
            "finalizer": {"verdict": "retry", "outcome": "Needs a new approach", "next_step": "retry"}})
    before = store.load(wid).model_dump(mode="json")
    with pytest.raises(ValueError):
        dispatch._claim(store, wid, "task")
    assert store.load(wid).model_dump(mode="json") == before


def test_losing_dispatch_controller_does_not_fail_the_winning_call(ready, monkeypatch):
    from app.assistant.dayflow_orchestrator import work_session
    store, wid, dispatch = ready
    dispatch._claim(store, wid, "task")
    before = store.load(wid).nodes["task"].model_dump(mode="json")
    monkeypatch.setattr(work_session, "open_session", lambda *a, **kw: pytest.fail("losing claimant must not open a session"))
    dispatch.blackboard.update_state_value("delegate_to", "work_emi_team_manager")
    dispatch.blackboard.update_state_value("acted_on_item_ids", [f"{wid}::task"])
    with pytest.raises(ValueError):
        dispatch.action_handler(SimpleNamespace())
    assert store.load(wid).nodes["task"].model_dump(mode="json") == before


def test_stale_dispatch_room_cannot_start_a_tool_for_a_newer_attempt(ready, monkeypatch):
    from unittest.mock import Mock
    from app.assistant.control_nodes import dayflow_tool_caller
    store, wid, dispatch = ready
    dispatch._claim(store, wid, "task")
    execute = Mock(return_value={"final_answer_answer": "wrong attempt"})
    monkeypatch.setattr(dayflow_tool_caller, "execute_dispatch", execute)
    caller = dayflow_tool_caller.DayflowToolCaller(name="caller", agent_registry={}, tool_registry={},
        blackboard=FakeBlackboard({"work_node_ref": f"{wid}::task", "dispatch_epoch": 0, "action": "test_tool"}))
    with pytest.raises(ValueError, match="stale"):
        caller.action_handler(SimpleNamespace())
    execute.assert_not_called()
