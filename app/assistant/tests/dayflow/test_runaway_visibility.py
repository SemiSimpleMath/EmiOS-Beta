"""A goal that is eating itself says so, and a finished node's subtree stops with it.

2026-09-13. A goal reading "record the October 9 Woodbridge PTSA Reflections deadline"
answered itself at 16:27 with a source URL and delivered that to the user by ticket at
16:58. It then grew to 117 nodes and ten levels of recursion by 17:40, burning about $4 an
hour against a $9.54 daily average, until it was stopped by hand.

Nothing caught it, and nothing was broken in the ordinary sense:

  * work_finalizer judges ONE completed node's result. It did its job, closed the research
    node, and wrote a correct epitaph.
  * strategic_planner_wo judges FIT, from a projection that lists only top-level tasks. It
    saw two subtasks and "progress: 1/2".
  * work_repair only adjudicates FAILED nodes. Nothing failed. Every step succeeded.

So two things were missing. There was no measure of the goal's own behaviour, and a node
judged complete did not stop the subtree beneath it: eight descendants of `done` and
`closed` ancestors were still running and still spawning children.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import pytest

from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio, work_signal
from work_objects.model import utcnow
from work_objects.store import WorkStore


@pytest.fixture
def store():
    path = os.path.join(tempfile.mkdtemp(), "work.db")
    st = WorkStore(path)
    yield st
    st.close()


def _goal(store, objective="Record the October 9 Reflections deadline."):
    return store.apply("create_work_object",
                       {"title": objective[:80], "goal_content": objective}, actor="steward")


def _child(store, wo_id, parent, title, status=None):
    wo = store.apply("add_node", {"work_id": wo_id, "type": "subtask", "title": title,
                                  "parent_id": parent}, actor="architect")
    node = [n for n in wo.nodes.values() if n.title == title][-1]
    if status:
        _set(store, wo_id, node.id, status)
    return node.id


def _set(store, wo_id, node_id, target, **extra):
    """Walk the legal path to `target`. The spine requires proposed -> dispatched -> done,
    so a test cannot jump straight to a terminal state."""
    path = {"done": ["dispatched", "done"],
            "closed": ["dispatched", "done", "closed"]}.get(target, [target])
    for step in path:
        data = {"work_id": wo_id, "node_id": node_id, "status": step}
        if step in ("closed", "abandoned", "superseded"):
            data["reason"] = extra.get("reason", "test")
        store.apply("set_status", data, actor=extra.get("actor", "worker"))


# --------------------------------------------------------------------------- #
# The subtree cascade
# --------------------------------------------------------------------------- #

def test_a_finished_node_stops_its_unstarted_subtree(store):
    wo = _goal(store)
    research = _child(store, wo.id, wo.goal_node_id, "Research the deadline")
    kid = _child(store, wo.id, research, "Verify the deadline")
    grandkid = _child(store, wo.id, kid, "Verify the verification")

    _set(store, wo.id, research, "done")

    after = store.load(wo.id)
    assert after.nodes[kid].status == "abandoned"
    assert after.nodes[grandkid].status == "abandoned", "the cascade must reach all the way down"
    assert after.nodes[kid].payload["terminal"]["verdict"] == "parent_finished"


def test_an_in_flight_worker_below_a_finished_node_is_left_to_land(store):
    """A dispatched worker owns a live thread; orphaning it mid-call loses the result."""
    wo = _goal(store)
    research = _child(store, wo.id, wo.goal_node_id, "Research the deadline")
    running = _child(store, wo.id, research, "Fetch the page", status="dispatched")

    _set(store, wo.id, research, "done")
    assert store.load(wo.id).nodes[running].status == "dispatched"


def test_a_dispatched_ask_below_a_finished_node_is_mooted(store):
    """An ask has no thread and no result to land, so the parent's completion ends it."""
    wo = _goal(store)
    research = _child(store, wo.id, wo.goal_node_id, "Research the deadline")
    ask = _child(store, wo.id, research, "Ask the user", status="dispatched")
    store.apply("defer_node", {"work_id": wo.id, "node_id": ask,
                               "wake_kind": "user_reply", "wake_ref": "q"}, actor="worker")

    _set(store, wo.id, research, "done")
    landed = store.load(wo.id).nodes[ask]
    assert landed.status == "abandoned"
    assert landed.wake_at is None and landed.wake_kind is None


def test_a_sibling_subtree_is_untouched(store):
    wo = _goal(store)
    a = _child(store, wo.id, wo.goal_node_id, "Branch A")
    b = _child(store, wo.id, wo.goal_node_id, "Branch B")
    a_kid = _child(store, wo.id, a, "A child")
    b_kid = _child(store, wo.id, b, "B child")

    _set(store, wo.id, a, "done")
    after = store.load(wo.id)
    assert after.nodes[a_kid].status == "abandoned"
    assert after.nodes[b_kid].status == "proposed", "only the finished branch stops"


# --------------------------------------------------------------------------- #
# The work signal
# --------------------------------------------------------------------------- #

def test_a_small_quiet_goal_gets_no_warnings(store):
    wo = _goal(store)
    _child(store, wo.id, wo.goal_node_id, "Do the thing")
    _child(store, wo.id, wo.goal_node_id, "Tell the user")
    rendered = render_work_portfolio(store.load(wo.id))
    assert "WORK SIGNAL" in rendered
    assert "⚠" not in rendered.split("WORK SIGNAL")[1]


def test_depth_is_reported_and_warned_on(store):
    wo = _goal(store)
    parent = wo.goal_node_id
    for i in range(6):
        parent = _child(store, wo.id, parent, f"Verify layer {i}")
    lines = "\n".join(work_signal(store.load(wo.id), utcnow()))
    assert "depth 6" in lines
    assert "levels below the goal" in lines


def test_a_burst_of_new_subtasks_is_called_out(store):
    wo = _goal(store)
    for i in range(12):
        _child(store, wo.id, wo.goal_node_id, f"Verify variant {i}")
    lines = "\n".join(work_signal(store.load(wo.id), utcnow()))
    assert "minted in the last" in lines
    assert "growing faster than it is finishing" in lines


def test_a_large_goal_is_called_out(store):
    wo = _goal(store)
    for i in range(30):
        _child(store, wo.id, wo.goal_node_id, f"Step {i}")
    lines = "\n".join(work_signal(store.load(wo.id), utcnow()))
    assert "30 subtasks for one goal" in lines


def test_an_old_quiet_goal_does_not_trip_the_burst_warning(store):
    """Age alone is not churn: a goal with parked work must not look like a runaway."""
    wo = _goal(store)
    for i in range(3):
        _child(store, wo.id, wo.goal_node_id, f"Step {i}")
    loaded = store.load(wo.id)
    future = utcnow() + timedelta(days=2)
    lines = "\n".join(work_signal(loaded, future))
    assert "created in the last hour" in lines
    assert "⚠" not in lines


def test_the_signal_survives_a_goal_with_no_subtasks(store):
    wo = _goal(store)
    assert work_signal(store.load(wo.id), utcnow()) == []
