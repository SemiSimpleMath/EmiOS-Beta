"""Stage-3 (finalizer authoritative) lifecycle guard.

Locks in the done->closed satisfaction flip: a worker-`done` node is NOT satisfied on its own — only the
finalizer's `closed` counts toward the goal, so nothing auto-completes a work object until the finalizer
has judged its results. Also covers the finalizer's RESOLVE close/abandon and the tool_success/user_signoff
re-key. These assertions mirror what work_finalizer_node._apply does to the store.
"""
import os

import pytest

from work_objects.store import WorkStore
from work_objects.model import new_id


@pytest.fixture()
def store(tmp_path):
    return WorkStore(os.path.join(str(tmp_path), "wo.db"))


def _mk(store, title, kind="all_owned_children_done"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title, "satisfied_when_kind": kind})
    return wo.id, wo.goal_node_id


def _sub(store, wid, gid, title, swk=None):
    nid = new_id("node")
    d = {"work_id": wid, "id": nid, "type": "subtask", "parent_id": gid, "title": title}
    if swk:
        d["satisfied_when_kind"] = swk
    store.apply("add_node", d, work_id=wid)
    return nid


def _step(store, wid, nid, *statuses):
    for st in statuses:
        d = {"work_id": wid, "node_id": nid, "status": st}
        if st in ("closed", "abandoned", "superseded"):
            d["reason"] = "test"
        store.apply("set_status", d, work_id=wid)


def _sat(store, wid, nid):
    wo = store.load(wid)
    return wo.is_satisfied(wo.nodes[nid])


def test_done_is_not_satisfied_only_closed_is(store):
    wid, gid = _mk(store, "A")
    a, b = _sub(store, wid, gid, "a"), _sub(store, wid, gid, "b")
    _step(store, wid, a, "actionable", "dispatched", "done")
    assert _sat(store, wid, a) is False                    # done alone does not satisfy
    assert _sat(store, wid, gid) is False
    assert store.load(wid).status == "active"              # no blind auto-complete
    _step(store, wid, a, "closed")                         # finalizer PROCEED
    assert _sat(store, wid, a) is True
    assert _sat(store, wid, gid) is False                  # sibling b still open


def test_goal_completes_only_when_all_children_closed(store):
    wid, gid = _mk(store, "A")
    a, b = _sub(store, wid, gid, "a"), _sub(store, wid, gid, "b")
    _step(store, wid, a, "actionable", "dispatched", "done", "closed")
    _step(store, wid, b, "actionable", "dispatched", "done", "closed")
    assert _sat(store, wid, gid) is True
    assert store.load(wid).status == "done"                # rollup completes on the last close


def test_resolve_close_completes_wo_with_open_sibling(store):
    wid, gid = _mk(store, "B")
    a, b = _sub(store, wid, gid, "a"), _sub(store, wid, gid, "open")
    _step(store, wid, a, "actionable", "dispatched", "done", "closed")
    store.apply("set_work_status", {"work_id": wid, "status": "done", "reason": "test"}, work_id=wid)
    wo = store.load(wid)
    assert wo.status == "done"
    assert wo.nodes[gid].status == "done"                  # goal mirrored
    # closure cascades: the open sibling is abandoned with the closure recorded — a
    # terminal work object may contain nothing startable (2026-07-30 zombie-wake fix)
    assert wo.nodes[b].status == "abandoned"
    assert wo.nodes[b].payload["terminal"]["reason"].startswith("work_object_done")


def test_resolve_abandon(store):
    wid, gid = _mk(store, "C")
    _sub(store, wid, gid, "a")
    store.apply("set_work_status", {"work_id": wid, "status": "abandoned", "reason": "test"}, work_id=wid)
    wo = store.load(wid)
    assert wo.status == "abandoned"
    assert wo.nodes[gid].status == "abandoned"


def test_tool_success_keyed_on_closed(store):
    wid, gid = _mk(store, "D")
    t = _sub(store, wid, gid, "tool", swk="tool_success")
    _step(store, wid, t, "actionable", "dispatched", "done")
    assert _sat(store, wid, t) is False                    # was satisfied-on-done pre-flip
    _step(store, wid, t, "closed")
    assert _sat(store, wid, t) is True
