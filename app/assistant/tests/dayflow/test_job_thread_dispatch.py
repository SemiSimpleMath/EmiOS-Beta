"""One thread per open task + tick-side supervision.

Worker dispatch claims the node synchronously (-> dispatched) and runs the worker on its own job
thread; the dispatching pass returns immediately. sweep_stuck_work_nodes supervises the in-flight
jobs each tick: ORPHANED (dispatched, no live thread — restart/crash) and FROZEN (thread alive but
no subtree/job activity past the timeout) both -> failed, for work_repair to adjudicate. The
transition machine fences zombie writes (failed -> done is illegal).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.assistant.dayflow_orchestrator.node_dispatch as nd
import app.assistant.dayflow_orchestrator.work_session as ws
from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
from app.assistant.dayflow_orchestrator.dispatch_sweeper import sweep_stuck_work_nodes
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Job WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _sub(store, wid, gid, node_id, status="actionable"):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask",
                             "parent_id": gid, "title": f"step {node_id}"})
    if status != "proposed":
        store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": status})
    return node_id


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestJobThread:

    def test_do_work_claims_then_runs_on_job_thread(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        ref = f"{wid}::n1"

        gate = threading.Event()
        signals = []
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        def slow_work_on(s, work_id, node_id=None, **kw):
            gate.wait(timeout=5)
            s.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "done"})
            return "done"

        import work_objects.discharge as wr
        monkeypatch.setattr(wr, "discharge_node", slow_work_on)

        ws.open_session(store, wid, "n1", "run_work_node")
        # Returned immediately: node claimed, job registered and alive, worker still running.
        assert store.load(wid).nodes["n1"].status == "dispatched"
        assert ws.session_alive(wid, "n1") is True

        gate.set()
        assert _wait_for(lambda: store.load(wid).nodes["n1"].status == "done")
        assert _wait_for(lambda: not ws.session_alive(wid, "n1"))
        assert signals == [ref]

    def test_job_crash_fails_the_node(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")

        import work_objects.discharge as wr
        def boom(*a, **kw):
            raise RuntimeError("worker exploded")
        monkeypatch.setattr(wr, "discharge_node", boom)

        ws.open_session(store, wid, "n1", "run_work_node")
        assert _wait_for(lambda: store.load(wid).nodes["n1"].status == "failed")


class TestSupervision:

    def test_orphaned_dispatched_node_is_failed(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})

        sweep_stuck_work_nodes()
        assert store.load(wid).nodes["n1"].status == "failed"

    def test_live_fresh_job_is_left_alone(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        sid = ws.session_id_for(wid, "n1")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc)}
        try:
            sweep_stuck_work_nodes()
            assert store.load(wid).nodes["n1"].status == "dispatched"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_frozen_job_is_failed_and_zombie_write_is_fenced(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        sid = ws.session_id_for(wid, "n1")
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True), "started_at": old}
        # Backdate the whole subtree so "no activity" holds.
        conn = getattr(store, "_conn", None)
        conn.execute("UPDATE nodes SET updated_at=? WHERE work_id=?", (old.isoformat(), wid))
        conn.commit()
        try:
            sweep_stuck_work_nodes()
            wo = store.load(wid)
            assert wo.nodes["n1"].status == "failed"
            # The abandoned zombie's late 'done' write is rejected by the transition machine.
            import pytest
            with pytest.raises(ValueError):
                store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "done"})
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_goal_node_is_skipped(self):
        store = _store()
        wid, gid = _mk_wo(store)
        store.apply("set_status", {"work_id": wid, "node_id": gid, "status": "dispatched"})

        sweep_stuck_work_nodes()
        assert store.load(wid).nodes[gid].status == "dispatched"

    def test_subtree_nodes_under_live_worker_are_not_orphaned(self):
        # 2026-07-31 regression: a worker grows sub-steps inside its own thread and marks
        # them dispatched; only the dispatch root has a registered job. The sweeper
        # orphan-failed the live run's subtree, repair re-issued a sub-step, and a
        # duplicate emi_team raced the first. Liveness must inherit up the parent_id spine.
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="dispatched")
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})
        store.apply("add_node", {"work_id": wid, "id": "n1a1", "type": "subtask",
                                 "parent_id": "n1a", "title": "worker sub-sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a1", "status": "dispatched"})
        sid = ws.session_id_for(wid, "n1")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc)}
        try:
            sweep_stuck_work_nodes()
            wo = store.load(wid)
            assert wo.nodes["n1"].status == "dispatched"
            assert wo.nodes["n1a"].status == "dispatched"
            assert wo.nodes["n1a1"].status == "dispatched"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_subtree_nodes_with_dead_root_job_are_failed(self):
        # No registered job anywhere on the spine (restart/crash) -> the root AND its
        # dispatched sub-steps all orphan-fail for work_repair.
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="dispatched")
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})

        sweep_stuck_work_nodes()
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "failed"
        assert wo.nodes["n1a"].status == "failed"

    def test_zombie_thread_does_not_shield_subtree_after_root_failed(self):
        # A frozen root was already failed by an earlier pass but its hung thread is
        # still alive. Coverage requires the ancestor to still be dispatched — the
        # zombie must not shield the abandoned subtree forever.
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="dispatched")
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "failed"})
        sid = ws.session_id_for(wid, "n1")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc)}
        try:
            sweep_stuck_work_nodes()
            assert store.load(wid).nodes["n1a"].status == "failed"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)


class TestOneDispatchPerTick:

    def test_dispatch_clears_acted_on_and_signals_more_ready(self, monkeypatch):
        dispatched = []
        signals = []
        monkeypatch.setattr(nd, "dispatch_node", lambda store, w, n, d: dispatched.append((w, n, d)))
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        bb = FakeBlackboard({
            "acted_on_item_ids": ["work_a::n1"],
            "delegate_to": "run_work_node",
            "actionable_items": [
                {"item_id": "work_a::n1"},
                {"item_id": "work_b::n2"},
            ],
        })
        node = WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        node.action_handler(message=None)

        assert dispatched == [("work_a", "n1", "run_work_node")]
        assert bb.get_state_value("acted_on_item_ids") == []
        # No loop-back: the state_map routes onward to finalize.
        assert bb.get_state_value("next_agent") is None
        assert signals == ["more_ready:1"]

    def test_no_signal_when_nothing_else_ready(self, monkeypatch):
        signals = []
        monkeypatch.setattr(nd, "dispatch_node", lambda *a: None)
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        bb = FakeBlackboard({
            "acted_on_item_ids": ["work_a::n1"],
            "delegate_to": "create_dayflow_ticket",
            "actionable_items": [{"item_id": "work_a::n1"}],
        })
        WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                             agent_registry={}, tool_registry={}).action_handler(message=None)
        assert signals == []
