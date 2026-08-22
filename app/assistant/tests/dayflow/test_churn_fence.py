"""The churn fence: silence cannot kill queued or held work (2026-08-22).

Three churn incidents (7, 6, and 33 generations) shared one mechanism: the
architect pruned nodes that were queued for dispatch or held for a future wake,
reading "hasn't run yet" as "will never run", and each re-mint went to the back
of the one-dispatch-per-tick queue. The store now refuses an `abandoned` write
on an `actionable` node or a future-wake `waiting` node unless the replan is
LICENSED — evidence (finalizer amend) or a user directive (steward-classed),
computed by the caller from typed flags, never from wording. Whole-object
closure (the steward's cascade) bypasses node-level writes and stays untouched.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
from work_objects.model import utcnow


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Fence WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _node(store, wid, gid, node_id, *, status=None, wake_at=None):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask",
                             "parent_id": gid, "title": f"step {node_id}"})
    if status:
        store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": status})
    if wake_at is not None:
        store.apply("defer_node", {"work_id": wid, "node_id": node_id,
                                   "wake_kind": "time", "wake_at": wake_at, "wake_ref": None})
    return node_id


class TestChurnFence:

    def test_unlicensed_abandon_of_queued_node_refused(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "queued", status="actionable")
        with pytest.raises(ValueError, match="queued is not stalled"):
            store.apply("set_status", {"work_id": wid, "node_id": "queued",
                                       "status": "abandoned", "reason": "replan says so"},
                        actor="architect")
        assert store.load(wid).nodes["queued"].status == "actionable"

    def test_unlicensed_abandon_of_held_node_refused(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "held", status="waiting", wake_at=utcnow() + timedelta(hours=2))
        with pytest.raises(ValueError, match="queued is not stalled"):
            store.apply("set_status", {"work_id": wid, "node_id": "held",
                                       "status": "abandoned", "reason": "looks stalled"},
                        actor="architect")

    def test_licensed_abandon_passes(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "queued", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "queued", "status": "abandoned",
                                   "licensed": True,
                                   "reason": "user declined this branch (ticket reply)"},
                    actor="architect")
        assert store.load(wid).nodes["queued"].status == "abandoned"

    def test_overdue_wake_is_not_held(self):
        """A PAST wake means the node is overdue, not held on purpose — prunable."""
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "overdue", status="waiting", wake_at=utcnow() - timedelta(hours=2))
        store.apply("set_status", {"work_id": wid, "node_id": "overdue", "status": "abandoned",
                                   "reason": "wake long past and moot"}, actor="architect")
        assert store.load(wid).nodes["overdue"].status == "abandoned"

    def test_proposed_node_stays_prunable(self):
        """`proposed` is the architect's own inbox — replanning unstarted plans is its job."""
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "planned")
        store.apply("set_status", {"work_id": wid, "node_id": "planned", "status": "abandoned",
                                   "reason": "superseded by a better step"}, actor="architect")
        assert store.load(wid).nodes["planned"].status == "abandoned"

    def test_cascade_bypasses_the_fence(self):
        """The steward's whole-object abandon (user: 'not your purview') clears queued
        nodes through the cascade — no license needed at WO level."""
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "queued", status="actionable")
        store.apply("set_work_status", {"work_id": wid, "status": "abandoned",
                                        "reason": "user declined the whole objective"},
                    actor="steward")
        assert store.load(wid).nodes["queued"].status == "abandoned"


class TestApplierLicenseFlow:

    def test_unlicensed_replan_prune_is_skipped_not_fatal(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "queued", status="actionable")
        res = apply_architect_dag(store, wid, [], abandon_node_ids=["queued"],
                                  abandon_reason="graph feels stale")
        assert res["abandoned"] == []
        assert store.load(wid).nodes["queued"].status == "actionable"

    def test_licensed_replan_prunes(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _node(store, wid, gid, "queued", status="actionable")
        res = apply_architect_dag(store, wid, [], abandon_node_ids=["queued"],
                                  abandon_reason="user said drop this branch", licensed=True)
        assert res["abandoned"] == ["queued"]
        assert store.load(wid).nodes["queued"].status == "abandoned"
