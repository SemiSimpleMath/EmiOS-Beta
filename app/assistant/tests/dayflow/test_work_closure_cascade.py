"""Closing a work object is a transition with obligations (2026-07-30 zombie-wake fix).

(test_work_finalizer_lifecycle.test_resolve_close_completes_wo_with_open_sibling asserted
the old label-write semantics — "open sibling untouched" — and was updated to the cascade.)

The incident: yesterday's dog-walk reminder object was marked done while one node was
still `waiting` with an armed next-day timer. Closure only flipped the object's status
label, the timer survived, and a ghost ticket fired a day later from inside a closed
object. The fix: entering done/abandoned cascade-abandons every startable node (wake
cleared), `_rollup` auto-completion carries the same obligation, `validate()` rejects
any terminal object holding a startable node, and `repair_terminal_zombies()` heals
rows written before the cascade existed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_objects.model import WorkNode, WorkObject
from work_objects.store import WorkStore


def _store(tmp_path) -> WorkStore:
    return WorkStore(str(tmp_path / "work.db"))


def _make_wo(store: WorkStore, title: str = "Notify the user to walk the dogs") -> WorkObject:
    return store.apply("create_work_object", {"title": title}, actor="test")


def _add(store: WorkStore, wo_id: str, node_id: str, status: str = "proposed", **fields):
    store.apply("add_node", {"work_id": wo_id, "id": node_id, "type": "subtask",
                             "title": node_id, **fields}, actor="test")
    if status != "proposed":
        store.apply("set_status", {"work_id": wo_id, "node_id": node_id, "status": status},
                    actor="test")


class TestClosureCascade:

    def test_set_work_status_done_cascades_startable_nodes(self, tmp_path):
        store = _store(tmp_path)
        wo = _make_wo(store)
        wake = datetime.now(timezone.utc) + timedelta(hours=22)
        _add(store, wo.id, "reminder", status="waiting")
        store.apply("defer_node", {"work_id": wo.id, "node_id": "reminder",
                                   "wake_kind": "time", "wake_at": wake.isoformat()}, actor="test")
        _add(store, wo.id, "prep")                       # proposed
        _add(store, wo.id, "broken", status="failed")

        closed = store.apply("set_work_status", {"work_id": wo.id, "status": "done"}, actor="steward")

        for node_id in ("reminder", "prep", "broken"):
            node = closed.nodes[node_id]
            assert node.status == "abandoned"
            assert node.wake_kind is None and node.wake_at is None and node.wake_ref is None
            assert node.payload["abandoned_reason"] == "work_object_done"
        assert closed.nodes[closed.goal_node_id].status == "done"   # goal mirror unchanged
        # the exact incident query: no startable node inside the terminal object
        assert not [n for n in closed.nodes.values()
                    if n.status in ("proposed", "actionable", "waiting", "failed")]

    def test_dispatched_node_survives_closure_and_lands_result(self, tmp_path):
        store = _store(tmp_path)
        wo = _make_wo(store)
        _add(store, wo.id, "inflight", status="dispatched")

        closed = store.apply("set_work_status", {"work_id": wo.id, "status": "abandoned"},
                             actor="steward")
        assert closed.nodes["inflight"].status == "dispatched"      # in-flight left to land

        landed = store.apply("set_status", {"work_id": wo.id, "node_id": "inflight",
                                            "status": "done"}, actor="worker")
        assert landed.nodes["inflight"].status == "done"            # result lands inert

    def test_validate_rejects_terminal_object_with_startable_node(self):
        wo = WorkObject(title="ghost", status="done")
        node = WorkNode(work_id=wo.id, type="subtask", status="waiting")
        wo.add_node(node)
        with pytest.raises(ValueError, match="startable"):
            wo.validate()

    def test_rollup_autoclose_cascades_stragglers(self, tmp_path):
        store = _store(tmp_path)
        wo = _make_wo(store)
        store.apply("add_node", {"work_id": wo.id, "id": "step", "type": "subtask",
                                 "title": "step", "parent_id": wo.goal_node_id,
                                 "satisfied_when_kind": "tool_success"}, actor="test")
        _add(store, wo.id, "straggler")                  # proposed root node, outside the goal subtree
        for status in ("dispatched", "done", "closed"):
            result = store.apply("set_status", {"work_id": wo.id, "node_id": "step",
                                                "status": status}, actor="test")
        assert result.status == "done"                   # rollup auto-completed the object
        assert result.nodes["straggler"].status == "abandoned"

    def test_repair_heals_pre_cascade_zombies(self, tmp_path):
        store = _store(tmp_path)
        wo = _make_wo(store)
        wake = datetime.now(timezone.utc) + timedelta(hours=22)
        _add(store, wo.id, "reminder", status="waiting")
        store.apply("defer_node", {"work_id": wo.id, "node_id": "reminder",
                                   "wake_kind": "time", "wake_at": wake.isoformat()}, actor="test")
        # manufacture the pre-fix state behind apply()'s back: closure as a label write
        # plus the old goal-node mirror — the rest of the DAG untouched
        with store._lock, store._conn:
            store._conn.execute("UPDATE work_objects SET status='done' WHERE id=?", (wo.id,))
            store._conn.execute("UPDATE nodes SET status='done' WHERE id=?", (wo.goal_node_id,))

        assert store.repair_terminal_zombies() == 1
        healed = store.load(wo.id)
        assert healed.nodes["reminder"].status == "abandoned"
        assert healed.nodes["reminder"].wake_at is None
        assert healed.nodes["reminder"].payload["abandoned_reason"] == "work_object_done"
        assert store.repair_terminal_zombies() == 0      # idempotent
        # the repair is itself event-logged
        ops = [e["op"] for e in store.events(wo.id)]
        assert "cascade_closure_repair" in ops
