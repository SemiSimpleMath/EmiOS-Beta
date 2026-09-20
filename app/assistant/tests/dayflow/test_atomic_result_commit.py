"""Crash and incarnation checks for the shared result commit (disposable in-memory DB)."""
from types import SimpleNamespace
import pytest
from work_objects.store import WorkStore
from work_objects.result_recorder import record_tool_result

@pytest.fixture
def task():
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "atomic result"})
    store.apply("add_node", {"work_id": wo.id, "id": "main", "type": "subtask", "parent_id": wo.goal_node_id})
    store.apply("set_status", {"work_id": wo.id, "node_id": "main", "status": "dispatched"})
    yield store, wo.id
    store.close()

def result(text="answer", pod="pod:new"):
    return SimpleNamespace(result_type="success", content=text, data={"pod_references": [{"pod_id": pod}]})

def test_stale_result_changes_nothing(task):
    store, wid = task
    before = store.load(wid).model_dump(mode="json")
    events = store.events(wid)
    assert not record_tool_result(store, wid, "main", result(), actor="test", expected_epoch=0)
    assert store.load(wid).model_dump(mode="json") == before
    assert store.events(wid) == events

def test_persistence_failure_rolls_back_status_pod_and_evidence(task, monkeypatch):
    store, wid = task
    before = store.load(wid).model_dump(mode="json")
    events = store.events(wid)
    original = store._persist
    def fail_on_evidence(wo, now):
        if any(n.type == "evidence" for n in wo.nodes.values()):
            original(wo, now)
            raise RuntimeError("simulated interruption")
        original(wo, now)
    monkeypatch.setattr(store, "_persist", fail_on_evidence)
    with pytest.raises(RuntimeError, match="interruption"):
        record_tool_result(store, wid, "main", result(), actor="test", expected_epoch=1)
    assert store.load(wid).model_dump(mode="json") == before
    assert store.events(wid) == events

def test_same_attempt_has_one_result(task):
    store, wid = task
    assert record_tool_result(store, wid, "main", result(), actor="test", expected_epoch=1)
    before = store.load(wid).model_dump(mode="json")
    assert not record_tool_result(store, wid, "main", result("duplicate", "pod:other"), actor="test", expected_epoch=1)
    assert store.load(wid).model_dump(mode="json") == before
    receipts = [n for n in store.load(wid).nodes.values() if n.type == "evidence"]
    assert len(receipts) == 1


def test_separate_store_connections_cannot_overwrite_each_others_graph(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    first = WorkStore(str(tmp_path / "concurrent.db"))
    wo = first.apply("create_work_object", {"title": "Concurrent graph edits"})
    second = WorkStore(str(tmp_path / "concurrent.db"))
    first_writing, release_first, second_loaded = Event(), Event(), Event()
    persist, load = first._persist, second._load
    def pause_persist(graph, now):
        first_writing.set()
        assert release_first.wait(5)
        return persist(graph, now)
    def observe_load(wid):
        graph = load(wid)
        second_loaded.set()
        return graph
    monkeypatch.setattr(first, "_persist", pause_persist)
    monkeypatch.setattr(second, "_load", observe_load)
    def add(store, nid):
        return store.apply("add_node", {"work_id": wo.id, "id": nid, "parent_id": wo.goal_node_id, "type": "subtask"})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(add, first, "first")
            assert first_writing.wait(5)
            b = pool.submit(add, second, "second")
            # Without a write transaction before load, B reads A's uncommitted predecessor.
            second_loaded.wait(0.3)
            release_first.set()
            a.result(timeout=5)
            result = b.result(timeout=5)
        assert {"first", "second"} <= set(result.nodes), "the second mutation must see the first committed graph"
    finally:
        release_first.set()
        first.close()
        second.close()


def test_idle_timeout_cannot_overwrite_new_subtree_activity(task):
    from datetime import datetime, timezone
    store, wid = task
    cutoff = datetime.now(timezone.utc)
    store.apply("add_node", {"work_id": wid, "type": "evidence", "parent_id": "main", "status": "assumed", "content": "fresh progress"})
    before = store.load(wid).model_dump(mode="json")
    assert not record_tool_result(store, wid, "main", result("timeout"), actor="sweeper", expected_epoch=1, idle_before=cutoff)
    assert store.load(wid).model_dump(mode="json") == before


def test_current_result_is_separate_from_prior_attempt_evidence(task):
    from app.assistant.dayflow_orchestrator.work_portfolio import node_result
    store, wid = task
    store.apply("add_node", {"work_id": wid, "type": "evidence", "parent_id": "main", "status": "assumed", "content": "old failure", "payload": {"dispatch_epoch": 0}})
    record_tool_result(store, wid, "main", result("current success"), actor="test", expected_epoch=1)
    wo = store.load(wid)
    assert "current success" in node_result(wo, wo.nodes["main"])
    assert "old failure" not in node_result(wo, wo.nodes["main"])
