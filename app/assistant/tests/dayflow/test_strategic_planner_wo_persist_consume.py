"""StrategicPlannerWoPersistNode consume path (B): a converted artifact's summary is folded into the
created work object's goal, and the intake item is closed."""
from __future__ import annotations

from app.assistant.control_nodes.strategic_planner_wo_persist_node import StrategicPlannerWoPersistNode
from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb):
    return StrategicPlannerWoPersistNode(
        name="strategic_planner_wo_persist_node",
        blackboard=bb, agent_registry={}, tool_registry={},
    )


def test_consumed_artifact_folds_into_goal_and_closes_item():
    seed_items([
        make_dayflow_message(item_id="email:flight1", short_id=700, state="actionable",
                             summary="Flight AA123 delayed to 6:40pm; rebooking link inside",
                             source_type="email"),
    ])

    # A freshly created work object (goal dispatched) — what persist_steward_output would have minted.
    store = get_dayflow_work_store()
    wo = store.apply("create_work_object",
                     {"title": "handle the delayed flight", "goal_content": "Handle the delayed flight"},
                     actor="test")
    store.apply("set_status", {"work_id": wo.id, "node_id": wo.goal_node_id, "status": "dispatched"}, actor="test")

    bb = FakeBlackboard({
        "admitted_artifacts": [
            {"metadata": {"item_id": "email:flight1", "short_id": "700",
                          "summary": "Flight AA123 delayed to 6:40pm; rebooking link inside"}},
        ],
    })
    node = _make_node(bb)

    # The created work object cites the artifact by short_id in based_on.
    node._close_consumed_items([{"work_id": wo.id, "based_on": ["700"]}])

    # Goal content now carries the originating intake (not just the one-line objective).
    reloaded = store.load(wo.id)
    goal = reloaded.nodes[reloaded.goal_node_id]
    assert "Flight AA123 delayed" in (goal.content or ""), f"intake not folded into goal: {goal.content!r}"
    assert "Originating intake" in goal.content
    assert goal.status == "dispatched", "fold must not change the goal status"

    # The item is closed (consumed).
    item = load_item_by_id("email:flight1")
    assert get_meta(item)["state"] == "closed"


def test_unconsumed_artifact_left_open():
    """An admitted artifact NOT cited in any based_on is left open (sits as context)."""
    seed_items([
        make_dayflow_message(item_id="email:newsletter", short_id=701, state="actionable",
                             summary="Weekly newsletter", source_type="email"),
    ])
    bb = FakeBlackboard({
        "admitted_artifacts": [
            {"metadata": {"item_id": "email:newsletter", "short_id": "701", "summary": "Weekly newsletter"}},
        ],
    })
    node = _make_node(bb)
    node._close_consumed_items([])  # nothing created -> nothing consumed

    item = load_item_by_id("email:newsletter")
    assert get_meta(item)["state"] == "actionable", "un-consumed artifact must stay open as context"
