"""Tests for PlannerPersistNode — persists planner output (tasks, synopses, closures)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.planner_persist_node import PlannerPersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    load_item_by_id,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> PlannerPersistNode:
    return PlannerPersistNode(
        name="planner_persist_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


class TestPersistPlannedTasks:

    def test_new_tasks_get_persisted(self):
        """Planner creates new tasks → they must appear in DB after persist."""
        # Seed an existing plan synopsis so the planner context makes sense.
        seed_items([
            make_plan_synopsis_message(plan_id="P1", synopsis="Test plan"),
        ])

        planned_tasks = [
            {
                "task_id": "new_1",
                "plan_id": "P1",
                "task": "Do the first thing",
                "dependencies": [],
            },
            {
                "task_id": "new_2",
                "plan_id": "P1",
                "task": "Do the second thing",
                "dependencies": ["new_1"],
            },
        ]

        # Build blackboard as the planner would leave it.
        bb = FakeBlackboard({
            "planned_tasks": planned_tasks,
            "plan_synopses": [{
                "plan_id": "P1",
                "is_new": False,
                "changed": False,
                "objective": "Test",
                "synopsis": "Test plan",
                "success_criteria": "",
                "step_outline": [],
            }],
            "completed_plan_ids": [],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": {},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        # planned_tasks should be cleared after persist.
        assert bb.get_state_value("planned_tasks") == []

        # Check DB for the new tasks.
        items = load_all_items()
        plan_tasks = [
            i for i in items
            if get_meta(i).get("source_type") == "plan_task"
            and get_meta(i).get("plan_id") == "P1"
        ]
        assert len(plan_tasks) == 2, f"Expected 2 plan tasks, got {len(plan_tasks)}"

        # They should have short_ids assigned.
        short_ids = [get_meta(t).get("short_id") for t in plan_tasks]
        assert all(sid is not None for sid in short_ids), f"Missing short_ids: {short_ids}"

    def test_existing_tasks_get_updated(self):
        """Planner references existing tasks → they get updated, not duplicated."""
        seed_items([
            make_dayflow_message(
                item_id="task:existing1", short_id=100, state="important_open",
                summary="Original summary", plan_id="P1", source_type="plan_task",
            ),
            make_plan_synopsis_message(plan_id="P1", synopsis="Test plan"),
        ])

        planned_tasks = [{
            "task_id": "100",  # References by short_id
            "plan_id": "P1",
            "task": "Updated summary for existing task",
            "dependencies": [],
        }]

        bb = FakeBlackboard({
            "planned_tasks": planned_tasks,
            "plan_synopses": [{
                "plan_id": "P1",
                "is_new": False,
                "changed": False,
                "objective": "Test",
                "synopsis": "Test plan",
            }],
            "completed_plan_ids": [],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": {"100": "task:existing1"},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        # Should still be exactly 1 plan_task (updated, not duplicated).
        items = load_all_items()
        plan_tasks = [
            i for i in items
            if get_meta(i).get("source_type") == "plan_task"
        ]
        assert len(plan_tasks) == 1
        meta = get_meta(plan_tasks[0])
        assert meta["short_id"] == 100  # Same short_id


class TestPersistSynopses:

    def test_new_synopsis_persisted(self):
        bb = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [{
                "plan_id": "P_new",
                "is_new": True,
                "changed": True,
                "objective": "New objective",
                "synopsis": "Brand new plan",
                "success_criteria": "It passes",
                "step_outline": ["s1", "s2"],
            }],
            "completed_plan_ids": [],
            "closed_task_ids": [],
            "existing_dayflow_items": [],
            "short_id_mapping": {},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("plan_synopsis:P_new")
        assert item is not None
        meta = get_meta(item)
        assert meta["synopsis"] == "Brand new plan"
        assert meta["objective"] == "New objective"

    def test_unchanged_synopsis_not_rewritten(self):
        """If changed=False and content matches, don't rewrite."""
        seed_items([
            make_plan_synopsis_message(plan_id="P_stable", synopsis="Stable plan",
                                       objective="Stable obj"),
        ])

        bb = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [{
                "plan_id": "P_stable",
                "is_new": False,
                "changed": False,
                "objective": "Stable obj",
                "synopsis": "Stable plan",
            }],
            "completed_plan_ids": [],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": {},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        # Should still exist with original content.
        item = load_item_by_id("plan_synopsis:P_stable")
        assert item is not None


class TestClosePlans:

    def test_completed_plan_closes_all_tasks(self):
        seed_items([
            make_plan_synopsis_message(plan_id="P_done"),
            make_dayflow_message(
                item_id="task:pd1", short_id=200, state="dispatched",
                plan_id="P_done", source_type="plan_task", summary="Task 1",
            ),
            make_dayflow_message(
                item_id="task:pd2", short_id=201, state="waiting",
                plan_id="P_done", source_type="plan_task", summary="Task 2",
            ),
        ])

        bb = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [],
            "completed_plan_ids": ["P_done"],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": {"200": "task:pd1", "201": "task:pd2"},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        # Both tasks should be closed now.
        for tid in ["task:pd1", "task:pd2"]:
            item = load_item_by_id(tid)
            meta = get_meta(item)
            assert meta["state"] == "closed", f"{tid} state={meta['state']}, expected closed"


class TestCancelTasks:

    def test_cancelled_task_gets_closed(self):
        seed_items([
            make_dayflow_message(
                item_id="task:cancel_me", short_id=300, state="waiting",
                summary="Cancel this", source_type="plan_task",
            ),
        ])

        bb = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [],
            "completed_plan_ids": [],
            "closed_task_ids": ["300"],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": {"300": "task:cancel_me"},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:cancel_me")
        meta = get_meta(item)
        assert meta["state"] == "closed"


class TestRoundTrip:

    def test_planner_creates_tasks_then_prep_sees_them(self):
        """Full round trip: planner persist → prep node reload → verify consistency."""
        from app.assistant.control_nodes.strategic_planner_prep_node import StrategicPlannerPrepNode

        # Step 1: Persist new plan + tasks via PlannerPersistNode
        bb = FakeBlackboard({
            "planned_tasks": [
                {"task_id": "rt_1", "plan_id": "P_rt", "task": "Round trip task 1", "dependencies": []},
                {"task_id": "rt_2", "plan_id": "P_rt", "task": "Round trip task 2", "dependencies": ["rt_1"]},
            ],
            "plan_synopses": [{
                "plan_id": "P_rt",
                "is_new": True,
                "changed": True,
                "objective": "Round trip test",
                "synopsis": "Testing round trip",
                "success_criteria": "Both tasks appear",
                "step_outline": ["rt_1", "rt_2"],
            }],
            "completed_plan_ids": [],
            "closed_task_ids": [],
            "existing_dayflow_items": [],
            "short_id_mapping": {},
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        persist_node = _make_node(bb)
        persist_node.action_handler(message=None)

        # Step 2: Run StrategicPlannerPrepNode on the same DB
        bb2 = FakeBlackboard({"admitted_artifacts": []})
        prep_node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb2,
            agent_registry={},
            tool_registry={},
        )
        prep_node.action_handler(message=None)

        # Step 3: Verify prep sees the tasks
        pts = bb2.get_state_value("plan_task_status")
        assert len(pts) >= 1, f"Expected at least 1 plan, got {len(pts)}"

        p_rt = next((p for p in pts if p["plan_id"] == "P_rt"), None)
        assert p_rt is not None, f"Plan P_rt not found in plan_task_status: {[p['plan_id'] for p in pts]}"

        total_tasks = len(p_rt["completed_tasks"]) + len(p_rt["remaining_tasks"])
        assert total_tasks == 2, f"Expected 2 tasks in plan, got {total_tasks}"
