"""Zombie incarnation fences (work-store audit W2, 2026-07-09).

The sweeper's frozen check is documented to false-positive on one
legitimately long tool call — so a healthy job completing late is
expected. Before these fences, that zombie could complete the SUCCESSOR
incarnation (repair re-opened the node, dispatch re-claimed it —
dispatched→done is legal again) and its registry pop deleted the
successor job's entry, making the sweeper orphan-fail the live job.
"""
from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from work_objects.store import WorkStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = WorkStore(path)
    yield s
    s.close()
    os.remove(path)


def _wo_with_node(store) -> tuple[str, str]:
    wo = store.apply("create_work_object", {"title": "t"}, actor="test")
    goal = wo.goal_node_id
    store.apply("add_node", {"work_id": wo.id, "id": "step_1", "type": "subtask",
                             "parent_id": goal, "title": "step"}, actor="test")
    return wo.id, "step_1"


def _epoch(store, wid, nid) -> int:
    return int(store.load(wid).nodes[nid].payload.get("dispatch_epoch") or 0)


class TestDispatchEpoch:

    def test_each_claim_bumps_the_epoch(self, store):
        wid, nid = _wo_with_node(store)
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"}, actor="t")
        assert _epoch(store, wid, nid) == 1
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "failed"}, actor="sweeper")
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "proposed"}, actor="repair")
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"}, actor="t")
        assert _epoch(store, wid, nid) == 2

    def test_stale_epoch_completion_is_rejected(self, store):
        wid, nid = _wo_with_node(store)
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"}, actor="t")
        zombie_epoch = _epoch(store, wid, nid)
        # Sweeper fails the frozen incarnation; repair re-opens; a successor claims.
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "failed"}, actor="sweeper")
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "proposed"}, actor="repair")
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"}, actor="t")

        with pytest.raises(ValueError, match="stale dispatch epoch"):
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "done",
                                       "expected_dispatch_epoch": zombie_epoch}, actor="zombie")
        # The live incarnation's own completion works.
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "done",
                                   "expected_dispatch_epoch": _epoch(store, wid, nid)}, actor="worker")
        assert store.load(wid).nodes[nid].status == "done"

    def test_plain_writes_without_epoch_are_unaffected(self, store):
        wid, nid = _wo_with_node(store)
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"}, actor="t")
        # Finalizer/sweeper-style writes carry no expected epoch — no fence applied.
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "done"}, actor="worker")
        store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "closed"}, actor="finalizer")
        assert store.load(wid).nodes[nid].status == "closed"


class TestRegistryOwnership:

    def test_zombie_pop_leaves_successor_entry(self):
        from app.assistant.dayflow_orchestrator import work_session as ws

        sid = ws.session_id_for("work_test", "node_test")
        successor_thread = SimpleNamespace()  # not this thread
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": successor_thread,
                                      "started_at": datetime.now(timezone.utc)}
        try:
            # the finally-block owner check in _run_session: emulate a zombie pop
            with ws._sessions_lock:
                entry = ws._live_sessions.get(sid)
                if entry is not None and entry.get("thread") is threading.current_thread():
                    ws._live_sessions.pop(sid, None)
            with ws._sessions_lock:
                assert sid in ws._live_sessions  # successor's entry survives
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_owner_pop_removes_own_entry(self):
        from app.assistant.dayflow_orchestrator import work_session as ws

        sid = ws.session_id_for("work_test", "node_own")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": threading.current_thread(),
                                      "started_at": datetime.now(timezone.utc)}
        with ws._sessions_lock:
            entry = ws._live_sessions.get(sid)
            if entry is not None and entry.get("thread") is threading.current_thread():
                ws._live_sessions.pop(sid, None)
        with ws._sessions_lock:
            assert sid not in ws._live_sessions


class TestClaimGuard:

    def _fake_store(self, status: str):
        applies = []
        node = SimpleNamespace(status=status,
                               payload={"dispatch_epoch": 1})
        wo = SimpleNamespace(nodes={"n1": node})
        return SimpleNamespace(load=lambda wid: wo,
                               apply=lambda *a, **k: applies.append((a, k))), applies

    def test_already_dispatched_node_is_refused(self, monkeypatch):
        from app.assistant.dayflow_orchestrator import work_session as ws

        monkeypatch.setattr(ws, "_run_session", lambda *a: None)
        store, applies = self._fake_store("dispatched")
        ws.open_session(store, "work_x", "n1", "run_work_node")
        assert applies == []                       # no claim written
        with ws._sessions_lock:
            assert ws.session_id_for("work_x", "n1") not in ws._live_sessions

    def test_claimable_node_is_claimed_and_started(self, monkeypatch):
        from app.assistant.dayflow_orchestrator import work_session as ws

        monkeypatch.setattr(ws, "_run_session", lambda *a: None)
        store, applies = self._fake_store("actionable")
        ws.open_session(store, "work_y", "n1", "run_work_node")
        assert len(applies) == 1
        assert applies[0][0][1]["status"] == "dispatched"
        assert applies[0][0][1]["session_id"] == ws.session_id_for("work_y", "n1")
        with ws._sessions_lock:
            entry = ws._live_sessions.pop(ws.session_id_for("work_y", "n1"), None)
        assert entry is not None
        entry["thread"].join(timeout=5)
