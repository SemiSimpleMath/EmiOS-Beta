"""End-to-end lifecycle tests for the dayflow pipeline.

Tests the full item lifecycle: new → triage → plan → dispatch → close,
verifying DB state at each step without any LLM calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.planner_persist_node import PlannerPersistNode
from app.assistant.control_nodes.post_room_finalize_node import PostRoomFinalizeNode
from app.assistant.control_nodes.strategic_planner_prep_node import StrategicPlannerPrepNode
from app.assistant.control_nodes.triage_persist_node import TriagePersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    load_item_by_id,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


class TestFullItemLifecycle:

    def test_item_survives_triage_plan_dispatch_close(self):
        """An item should be traceable through every pipeline stage.

        Stage 1: Triage admits an email artifact.
        Stage 2: Planner creates a plan with tasks for it.
        Stage 3: Planner prep sees the tasks.
        Stage 4: Plan gets completed, tasks closed.
        """
        now = datetime.now(timezone.utc)

        # ── Stage 1: Triage persist ──────────────────────────────
        artifact = {
            "id": "email:lifecycle1",
            "metadata": {
                "item_id": "email:lifecycle1",
                "source_type": "email",
                "event_type": "email_received",
                "state": "artifact",
                "summary": "TurboTax filing reminder",
                "importance": "high",
                "created_at": now.isoformat(),
            },
        }

        bb_triage = FakeBlackboard({"admitted_artifacts": [artifact]})
        triage_node = TriagePersistNode(
            name="triage_persist_node",
            blackboard=bb_triage,
            agent_registry={},
            tool_registry={},
        )
        triage_node.action_handler(message=None)

        # Verify artifact is in DB
        item = load_item_by_id("email:lifecycle1")
        assert item is not None
        assert get_meta(item)["state"] == "artifact"

        # ── Stage 2: Planner creates tasks ───────────────────────
        bb_plan = FakeBlackboard({
            "planned_tasks": [
                {"task_id": "lt1", "plan_id": "plan_taxes", "task": "Review TurboTax forms",
                 "dependencies": []},
                {"task_id": "lt2", "plan_id": "plan_taxes", "task": "Upload missing docs",
                 "dependencies": ["lt1"]},
                {"task_id": "lt3", "plan_id": "plan_taxes", "task": "File return",
                 "dependencies": ["lt2"]},
            ],
            "plan_synopses": [{
                "plan_id": "plan_taxes",
                "is_new": True,
                "changed": True,
                "objective": "File 2025 taxes before Apr 15",
                "synopsis": "Review, gather docs, file",
                "success_criteria": "Return filed",
                "step_outline": ["review", "gather", "file"],
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

        persist_node = PlannerPersistNode(
            name="planner_persist_node",
            blackboard=bb_plan,
            agent_registry={},
            tool_registry={},
        )
        persist_node.action_handler(message=None)

        # Verify tasks are in DB
        items = load_all_items()
        plan_tasks = [
            i for i in items
            if get_meta(i).get("source_type") == "plan_task"
            and get_meta(i).get("plan_id") == "plan_taxes"
        ]
        assert len(plan_tasks) == 3, f"Expected 3 tasks, got {len(plan_tasks)}"

        # Verify synopsis is in DB
        synopsis = load_item_by_id("plan_synopsis:plan_taxes")
        assert synopsis is not None

        # ── Stage 3: Planner prep sees tasks ─────────────────────
        bb_prep = FakeBlackboard({"admitted_artifacts": []})
        prep_node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb_prep,
            agent_registry={},
            tool_registry={},
        )
        prep_node.action_handler(message=None)

        pts = bb_prep.get_state_value("plan_task_status")
        plan_taxes = next((p for p in pts if p["plan_id"] == "plan_taxes"), None)
        assert plan_taxes is not None
        total_tasks = len(plan_taxes["completed_tasks"]) + len(plan_taxes["remaining_tasks"])
        assert total_tasks == 3

        # ── Stage 4: Complete the plan ───────────────────────────
        # Get the canonical item_ids for the tasks
        task_ids = [get_meta(t)["item_id"] for t in plan_tasks]
        short_id_map = {
            str(get_meta(t).get("short_id")): get_meta(t)["item_id"]
            for t in plan_tasks
        }

        bb_close = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [],
            "completed_plan_ids": ["plan_taxes"],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": short_id_map,
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        close_node = PlannerPersistNode(
            name="planner_persist_node",
            blackboard=bb_close,
            agent_registry={},
            tool_registry={},
        )
        close_node.action_handler(message=None)

        # Verify all tasks are closed
        for tid in task_ids:
            item = load_item_by_id(tid)
            meta = get_meta(item)
            assert meta["state"] == "closed", \
                f"Task {tid} (sid:{meta.get('short_id')}) should be closed, got {meta['state']}"

    def test_multiple_plans_independent(self):
        """Two plans should not interfere with each other."""
        bb = FakeBlackboard({
            "planned_tasks": [
                {"task_id": "a1", "plan_id": "P_A", "task": "Plan A task", "dependencies": []},
                {"task_id": "b1", "plan_id": "P_B", "task": "Plan B task", "dependencies": []},
            ],
            "plan_synopses": [
                {"plan_id": "P_A", "is_new": True, "changed": True, "objective": "A", "synopsis": "Plan A"},
                {"plan_id": "P_B", "is_new": True, "changed": True, "objective": "B", "synopsis": "Plan B"},
            ],
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

        node = PlannerPersistNode(
            name="planner_persist_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        items = load_all_items()
        tasks_a = [i for i in items if get_meta(i).get("plan_id") == "P_A" and get_meta(i).get("source_type") == "plan_task"]
        tasks_b = [i for i in items if get_meta(i).get("plan_id") == "P_B" and get_meta(i).get("source_type") == "plan_task"]
        assert len(tasks_a) == 1
        assert len(tasks_b) == 1

        # Closing plan A should not affect plan B
        short_ids_a = {str(get_meta(t).get("short_id")): get_meta(t)["item_id"] for t in tasks_a}
        bb2 = FakeBlackboard({
            "planned_tasks": [],
            "plan_synopses": [],
            "completed_plan_ids": ["P_A"],
            "closed_task_ids": [],
            "existing_dayflow_items": load_all_items(),
            "short_id_mapping": short_ids_a,
            "state_mutations": [],
            "state_mutations_persisted_tf": False,
            "acted_on_item_ids": [],
            "active_dispatch_records": [],
            "action_result_events": [],
            "reasoning": "",
        })

        node2 = PlannerPersistNode(
            name="planner_persist_node",
            blackboard=bb2,
            agent_registry={},
            tool_registry={},
        )
        node2.action_handler(message=None)

        # Plan A task closed
        for t in tasks_a:
            item = load_item_by_id(get_meta(t)["item_id"])
            assert get_meta(item)["state"] == "closed"

        # Plan B task untouched
        for t in tasks_b:
            item = load_item_by_id(get_meta(t)["item_id"])
            assert get_meta(item)["state"] != "closed"


class TestStateTransitionLifecycle:

    def test_important_open_to_actionable_to_dispatched_to_closed(self):
        """Test the happy path state transitions via write_dayflow_item."""
        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item

        seed_items([
            make_dayflow_message(item_id="task:lifecycle_st", short_id=1,
                                state="important_open", summary="Lifecycle task"),
        ])

        write_dayflow_item("task:lifecycle_st", state="actionable", reason="Ready", caller="test")
        assert get_meta(load_item_by_id("task:lifecycle_st"))["state"] == "actionable"

        write_dayflow_item("task:lifecycle_st", state="dispatched", reason="Sent to user", caller="test")
        assert get_meta(load_item_by_id("task:lifecycle_st"))["state"] == "dispatched"

        write_dayflow_item("task:lifecycle_st", state="closed", reason="Done", caller="test")
        assert get_meta(load_item_by_id("task:lifecycle_st"))["state"] == "closed"

    def test_actionable_to_waiting_to_actionable(self):
        """Test the waiting → reactivation path."""
        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item

        seed_items([
            make_dayflow_message(item_id="task:wait_cycle", state="actionable"),
        ])

        write_dayflow_item(
            "task:wait_cycle",
            state="waiting",
            updates={
                "wait_reason": "Need user input",
                "reactivate_at_utc": "2026-04-10T10:00:00Z",
            },
            reason="Blocked",
            caller="test",
        )
        meta = get_meta(load_item_by_id("task:wait_cycle"))
        assert meta["state"] == "waiting"
        assert meta.get("wait_reason") == "Need user input"

        write_dayflow_item("task:wait_cycle", state="actionable", reason="Timer fired", caller="test")
        assert get_meta(load_item_by_id("task:wait_cycle"))["state"] == "actionable"
