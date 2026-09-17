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


# --------------------------------------------------------------------------- #
# The failure tally the re-plan cannot launder
# --------------------------------------------------------------------------- #
# 2026-09-17. The per-node failure_count is reset by the very act of continuing: the architect
# abandons a failing node and mints its replacement under a fresh slug, and the replacement
# starts at zero. A picture-day goal walked around the >=2 ceiling all day this way — every
# per-node counter read 0 or 1 while the goal had been failing at the same thing since morning.
# The goal node outlives every child, so the goal's own tally lives there.

def _goal_fails(store, wo_id):
    wo = store.load(wo_id)
    return int((wo.nodes[wo.goal_node_id].payload or {}).get("goal_unmet_attempts") or 0)


def test_the_goal_counts_failures_its_nodes_do_not_survive(store):
    """Two DIFFERENT nodes failing once each is a goal that has failed twice."""
    wo = _goal(store, "Get the picture packet.")
    first = _child(store, wo.id, wo.goal_node_id, "Contact the school")
    _set(store, wo.id, first, "failed")
    assert _goal_fails(store, wo.id) == 1

    # The architect abandons the failure and re-plans the same work under a new slug.
    _set(store, wo.id, first, "abandoned", actor="steward", reason="replaced")
    second = _child(store, wo.id, wo.goal_node_id, "Reach the school another way")
    _set(store, wo.id, second, "failed")

    wo2 = store.load(wo.id)
    assert int((wo2.nodes[second].payload or {}).get("failure_count") or 0) == 1, (
        "the replacement node has genuinely failed only once")
    assert _goal_fails(store, wo.id) == 2, (
        "but the GOAL has now failed twice — the count the re-plan cannot reset")


def test_the_architect_is_told_when_the_goal_keeps_failing(store):
    from app.assistant.control_nodes.work_architect_node import _render_existing_graph
    wo = _goal(store, "Get the picture packet.")
    for title in ("Contact the school", "Reach the school another way"):
        nid = _child(store, wo.id, wo.goal_node_id, title)
        _set(store, wo.id, nid, "failed")

    rendered = _render_existing_graph(store.load(wo.id))
    assert "2 ATTEMPTS HAVE NOT ACHIEVED THIS GOAL" in rendered
    assert "ask whether" in rendered


def test_one_failure_is_not_worth_shouting_about(store):
    from app.assistant.control_nodes.work_architect_node import _render_existing_graph
    wo = _goal(store, "Get the picture packet.")
    nid = _child(store, wo.id, wo.goal_node_id, "Contact the school")
    _set(store, wo.id, nid, "failed")

    assert "HAVE NOT ACHIEVED THIS GOAL" not in _render_existing_graph(store.load(wo.id))


def test_a_goal_that_never_fails_carries_no_tally(store):
    wo = _goal(store, "Get the picture packet.")
    nid = _child(store, wo.id, wo.goal_node_id, "Contact the school")
    _set(store, wo.id, nid, "done")
    assert _goal_fails(store, wo.id) == 0


def test_re_entering_failed_does_not_double_count(store):
    """The tally counts transitions INTO failed, like the per-node count it accompanies."""
    wo = _goal(store, "Get the picture packet.")
    nid = _child(store, wo.id, wo.goal_node_id, "Contact the school")
    _set(store, wo.id, nid, "failed")
    store.apply("set_status", {"work_id": wo.id, "node_id": nid, "status": "failed"}, actor="worker")
    assert _goal_fails(store, wo.id) == 1


def test_an_amend_counts_as_an_attempt_that_did_not_land(store):
    """The shape that got nowhere all day: the worker comes back having done PART of the job.

    Nothing crashes. The finalizer judges `amend`, the node closes, and if only crashes were
    counted the goal would look flawless while achieving nothing four times running.
    """
    wo = _goal(store, "Get the picture packet.")
    for title in ("Find the packet", "Find the packet another way"):
        nid = _child(store, wo.id, wo.goal_node_id, title)
        _set(store, wo.id, nid, "done")
        store.apply("set_status", {
            "work_id": wo.id, "node_id": nid, "status": "closed",
            "reason": "judged", "finalizer": {"verdict": "amend", "instruction": "get the packet",
                                              "reasoning": "date confirmed, packet not found"},
        }, actor="finalizer")

    assert _goal_fails(store, wo.id) == 2, "two attempts, neither achieved the goal"
    for n in store.load(wo.id).nodes.values():
        assert int((n.payload or {}).get("failure_count") or 0) == 0, "nothing ever crashed"


def test_proceed_is_not_an_unmet_attempt(store):
    wo = _goal(store, "Get the picture packet.")
    nid = _child(store, wo.id, wo.goal_node_id, "Find the packet")
    _set(store, wo.id, nid, "done")
    store.apply("set_status", {"work_id": wo.id, "node_id": nid, "status": "closed",
                               "reason": "judged",
                               "finalizer": {"verdict": "proceed", "instruction": "",
                                             "reasoning": "found it"}}, actor="finalizer")
    assert _goal_fails(store, wo.id) == 0


def test_the_architect_is_told_to_stop_and_ask_after_two_amends(store):
    from app.assistant.control_nodes.work_architect_node import _render_existing_graph
    wo = _goal(store, "Get the picture packet.")
    for title in ("Find the packet", "Find the packet another way"):
        nid = _child(store, wo.id, wo.goal_node_id, title)
        _set(store, wo.id, nid, "done")
        store.apply("set_status", {"work_id": wo.id, "node_id": nid, "status": "closed",
                                   "reason": "judged",
                                   "finalizer": {"verdict": "amend", "instruction": "keep looking",
                                                 "reasoning": "not found"}}, actor="finalizer")

    rendered = _render_existing_graph(store.load(wo.id))
    assert "2 ATTEMPTS HAVE NOT ACHIEVED THIS GOAL" in rendered
    assert "Stop trying" in rendered
