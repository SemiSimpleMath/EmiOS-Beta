"""The architect cannot dispose of a failed node on its own (2026-09-02).

The spend-alert loop: an ask timed out, the sweeper failed it, and the architect —
whose prompt claimed failed nodes "come back to you" — abandoned it and minted a
fresh identical ask. Hourly. Nine times. The component that owned the re-ask
decision never saw a single case: the architect consumed each failure first.

The store refuses a set_status on a `failed` node when the actor is the architect
and the replan is not LICENSED (the finalizer's verdict on that node / a user
directive). work_repair is retired (2026-09-16), but the fence outlives it: the
finalizer's route is now what licenses the architect. Other actors' legal traffic —
retry (failed->dispatched), re-open (failed->proposed), abandon with epitaph —
passes untouched.
"""
from __future__ import annotations

import pytest


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _failed_ask(store, title="Repair-owned WO", nid="ask"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    wid, gid = wo.id, wo.goal_node_id
    store.apply("add_node", {"work_id": wid, "id": nid, "type": "subtask",
                             "parent_id": gid, "title": "ask the user"})
    store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "dispatched"},
                actor="node_dispatch")
    store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "failed",
                               "note": "ask timed out (ticket expired unanswered)"},
                actor="dispatch_sweeper")
    return wid


class TestFailedNodesAreRepairs:
    def test_architect_cannot_abandon_a_failed_node(self):
        store = _store()
        wid = _failed_ask(store, nid="ask_a")
        with pytest.raises(ValueError, match="licensed replan"):
            store.apply("set_status", {"work_id": wid, "node_id": "ask_a",
                                       "status": "abandoned", "verdict": "pruned_by_replan",
                                       "reason": "replace with a fresh identical ask"},
                        actor="architect")
        assert store.load(wid).nodes["ask_a"].status == "failed", \
            "the failed node must still be sitting there carrying its verdict"

    def test_licensed_replan_may_still_prune_a_failed_branch(self):
        store = _store()
        wid = _failed_ask(store, nid="ask_b")
        store.apply("set_status", {"work_id": wid, "node_id": "ask_b",
                                   "status": "abandoned", "licensed": True,
                                   "reason": "user directive: stop asking about this"},
                    actor="architect")
        assert store.load(wid).nodes["ask_b"].status == "abandoned"

    def test_repair_adjudication_passes_untouched(self):
        store = _store()
        # retry: failed -> dispatched
        wid = _failed_ask(store, nid="ask_c")
        store.apply("set_status", {"work_id": wid, "node_id": "ask_c",
                                   "status": "dispatched"}, actor="work_repair")
        assert store.load(wid).nodes["ask_c"].status == "dispatched"
        # abandon with epitaph
        wid2 = _failed_ask(store, nid="ask_d")
        store.apply("set_status", {"work_id": wid2, "node_id": "ask_d",
                                   "status": "abandoned", "verdict": "repair_abandon",
                                   "reason": "user unreachable after adjudication; not re-asking"},
                    actor="work_repair")
        assert store.load(wid2).nodes["ask_d"].status == "abandoned"

    def test_architect_untouched_on_non_failed_nodes(self):
        """The fence is about failed nodes only — an ordinary unlicensed prune of a
        proposed node (not queued, not held) stays legal for the architect."""
        store = _store()
        wo = store.apply("create_work_object", {"title": "t", "goal_content": "t",
                                                "satisfied_when_kind": "all_owned_children_done"})
        store.apply("add_node", {"work_id": wo.id, "id": "draft", "type": "subtask",
                                 "parent_id": wo.goal_node_id, "title": "a proposed step"})
        store.apply("set_status", {"work_id": wo.id, "node_id": "draft",
                                   "status": "abandoned", "verdict": "pruned_by_replan",
                                   "reason": "superseded by a better chain in this replan"},
                    actor="architect")
        assert store.load(wo.id).nodes["draft"].status == "abandoned"
