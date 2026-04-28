"""Diagnostic tests: what does each prompt section actually see?

Seeds realistic items and dumps the prepared blackboard data
to verify what's visible vs. hidden in each prompt section.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.strategic_planner_prep_node import StrategicPlannerPrepNode
from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    make_action_log_message,
    make_chat_message,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


def _seed_realistic_scenario():
    """Seed a realistic dayflow state with multiple plans, states, and source types."""
    now = datetime.now(timezone.utc)
    items = [
        # ── Active plan with mixed-state tasks ──
        make_plan_synopsis_message(plan_id="plan_taxes", synopsis="File 2025 taxes before Apr 15"),
        make_dayflow_message(
            item_id="task:t1", short_id=381, state="dispatched", plan_id="plan_taxes",
            source_type="plan_task", summary="Review TurboTax pre-filled forms",
            extra_meta={"execution_result": "Checklist of missing items generated",
                        "dispatched_at": (now - timedelta(hours=1)).isoformat()},
        ),
        make_dayflow_message(
            item_id="task:t2", short_id=382, state="dispatched", plan_id="plan_taxes",
            source_type="plan_task", summary="Upload missing W-2s and 1099s",
            extra_meta={"dispatched_at": (now - timedelta(minutes=30)).isoformat()},
        ),
        make_dayflow_message(
            item_id="task:t3", short_id=384, state="waiting", plan_id="plan_taxes",
            source_type="plan_task", summary="File return via TurboTax",
            extra_meta={"wait_reason": "Blocked on doc upload",
                        "reactivate_at": (now + timedelta(hours=2)).isoformat()},
        ),
        make_dayflow_message(
            item_id="task:t_closed_in_plan", short_id=380, state="closed", plan_id="plan_taxes",
            source_type="plan_task", summary="Check email for TurboTax link",
            last_reviewed_at=now - timedelta(minutes=45),
        ),

        # ── Closed plan ──
        make_plan_synopsis_message(plan_id="plan_morning", synopsis="Morning routine check-ins",
                                   state="closed"),
        make_dayflow_message(
            item_id="task:m1", short_id=370, state="closed", plan_id="plan_morning",
            source_type="plan_task", summary="Morning clock-in check",
        ),
        make_dayflow_message(
            item_id="task:m2", short_id=371, state="closed", plan_id="plan_morning",
            source_type="plan_task", summary="Dog walk reminder",
        ),

        # ── Non-plan open tasks ──
        make_dayflow_message(
            item_id="task:np1", short_id=357, state="dispatched",
            source_type="plan_task", summary="Review Sam's English submissions",
            extra_meta={"dispatched_at": (now - timedelta(hours=2)).isoformat()},
        ),
        make_dayflow_message(
            item_id="task:np2", short_id=368, state="waiting",
            source_type="plan_task", summary="Read Mediabistro WGA email",
            extra_meta={"wait_reason": "Deferred to personal-coding window",
                        "reactivate_at": (now + timedelta(hours=4)).isoformat()},
        ),

        # ── Non-plan closed tasks ──
        make_dayflow_message(
            item_id="task:np_closed1", short_id=350, state="closed",
            source_type="plan_task", summary="Set thermostat to 75F",
            last_reviewed_at=now - timedelta(minutes=30),
        ),
        make_dayflow_message(
            item_id="task:np_closed2", short_id=351, state="closed",
            source_type="email", summary="Responded to team standup email",
            last_reviewed_at=now - timedelta(minutes=20),
        ),

        # ── Artifacts (no plan) ──
        make_dayflow_message(
            item_id="email:artifact1", short_id=389, state="artifact",
            source_type="email", summary="LinkedIn job alert: CorVel Data Scientist II",
        ),

        # ── Chat messages ──
        make_chat_message(summary="User: I'm working on Claude Code stuff", short_id=374,
                          created_at=now - timedelta(minutes=15)),
        make_chat_message(summary="User: Katy already took the dogs", short_id=375,
                          created_at=now - timedelta(hours=3)),

        # ── Action logs ──
        make_action_log_message(summary="Dispatched to devices_manager: lights off",
                                created_at=now - timedelta(minutes=10)),

        # ── Suppressed item (should be invisible) ──
        make_dayflow_message(
            item_id="task:suppressed1", short_id=360, state="suppressed",
            summary="Spam notification", source_type="email",
        ),
    ]
    seed_items(items)


class TestPlannerPromptVisibility:
    """What does the strategic planner prompt see?"""

    def test_dump_planner_prompt_data(self):
        _seed_realistic_scenario()

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        # ── plan_task_status (CURRENT PLANS + RECENTLY CLOSED PLANS) ──
        pts = bb.get_state_value("plan_task_status")
        assert pts is not None

        active_plans = []
        closed_plans = []
        for plan in pts:
            has_remaining = len(plan.get("remaining_tasks", [])) > 0
            if plan.get("plan_state", "active") in ("closed", "suppressed") and not has_remaining:
                closed_plans.append(plan)
            else:
                active_plans.append(plan)

        # Active plan: plan_taxes should be here with all 4 tasks
        assert any(p["plan_id"] == "plan_taxes" for p in active_plans), \
            f"plan_taxes not in active_plans: {[p['plan_id'] for p in active_plans]}"

        taxes_plan = next(p for p in active_plans if p["plan_id"] == "plan_taxes")
        all_tasks = taxes_plan["completed_tasks"] + taxes_plan["remaining_tasks"]
        assert len(all_tasks) == 4, \
            f"Expected 4 tasks in plan_taxes, got {len(all_tasks)}: {[t['task_id'] for t in all_tasks]}"

        # Closed task (380) should be in completed_tasks
        completed_ids = [t["task_id"] for t in taxes_plan["completed_tasks"]]
        assert any("380" in str(tid) or "t_closed" in str(tid) for tid in completed_ids), \
            f"Closed task 380 not in completed_tasks: {completed_ids}"

        # Closed plan: plan_morning should be here
        assert any(p["plan_id"] == "plan_morning" for p in closed_plans), \
            f"plan_morning not in closed_plans: {[p['plan_id'] for p in closed_plans]}"

        # ── active_dayflow_items (NON-PLAN OPEN + CLOSED TASKS) ──
        active_items = bb.get_state_value("active_dayflow_items")
        assert active_items is not None

        # Split like the template does
        non_plan_open = []
        non_plan_closed = []
        for ex in active_items:
            m = get_meta(ex)
            if not m.get("plan_id"):
                state = m.get("state", "")
                if state in ("important_open", "actionable", "dispatched", "waiting", "watching"):
                    non_plan_open.append(m)
                elif state == "closed":
                    non_plan_closed.append(m)

        # Non-plan open: np1 (dispatched) and np2 (waiting)
        open_sids = [m.get("short_id") for m in non_plan_open]
        assert 357 in open_sids or "357" in [str(s) for s in open_sids], \
            f"Task 357 not in non_plan_open: sids={open_sids}"
        assert 368 in open_sids or "368" in [str(s) for s in open_sids], \
            f"Task 368 not in non_plan_open: sids={open_sids}"

        # Non-plan closed: np_closed1 and np_closed2 (both closed)
        closed_sids = [m.get("short_id") for m in non_plan_closed]
        assert 350 in closed_sids or "350" in [str(s) for s in closed_sids], \
            f"Task 350 not in non_plan_closed: sids={closed_sids}"
        assert 351 in closed_sids or "351" in [str(s) for s in closed_sids], \
            f"Task 351 not in non_plan_closed: sids={closed_sids}"

        # ── Suppressed items should be invisible ──
        all_item_ids = [get_meta(i).get("item_id") for i in active_items]
        assert "task:suppressed1" not in all_item_ids, \
            "Suppressed item should not appear in active_dayflow_items"

        # ── Chat history ──
        chat = bb.get_state_value("recent_dayflow_chat_history")
        assert len(chat) >= 1
        # Recent chat (15 min ago) should be there
        assert any("Claude Code" in c["summary"] for c in chat)

        # ── Chat should NOT be in active_dayflow_items ──
        source_types = {get_meta(i).get("source_type") for i in active_items}
        assert "chat" not in source_types
        assert "action_log" not in source_types

    def test_closed_tasks_in_plan_visible_as_completed(self):
        """A closed task inside an active plan must appear in completed_tasks."""
        seed_items([
            make_plan_synopsis_message(plan_id="P1", synopsis="Test plan"),
            make_dayflow_message(
                item_id="task:done", short_id=10, state="closed",
                plan_id="P1", source_type="plan_task", summary="Completed step",
            ),
            make_dayflow_message(
                item_id="task:open", short_id=11, state="dispatched",
                plan_id="P1", source_type="plan_task", summary="Still working",
            ),
        ])

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        pts = bb.get_state_value("plan_task_status")
        p1 = next(p for p in pts if p["plan_id"] == "P1")
        assert len(p1["completed_tasks"]) == 1
        assert len(p1["remaining_tasks"]) == 1
        assert p1["completed_tasks"][0]["state"] == "closed"

    def test_closed_tasks_with_result_in_plan_visible_as_completed(self):
        """Closed tasks (with or without execution_result) are in completed_tasks."""
        seed_items([
            make_plan_synopsis_message(plan_id="P1"),
            make_dayflow_message(
                item_id="task:done_with_result", short_id=20, state="closed",
                plan_id="P1", source_type="plan_task", summary="Task with result",
                extra_meta={"execution_result": "Successfully completed"},
            ),
        ])

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        pts = bb.get_state_value("plan_task_status")
        p1 = next(p for p in pts if p["plan_id"] == "P1")
        assert len(p1["completed_tasks"]) == 1
        assert p1["completed_tasks"][0]["state"] == "closed"


class TestStateMoverPromptVisibility:
    """What does the state_mover prompt see?"""

    def test_state_mover_excludes_closed_synopses(self):
        _seed_realistic_scenario()

        bb = FakeBlackboard()
        node = StateMoverPrepNode(
            name="state_mover_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        synopses = bb.get_state_value("active_plan_synopses")
        plan_ids = [s.get("plan_id") for s in synopses]
        assert "plan_taxes" in plan_ids, "Active plan should be visible"
        assert "plan_morning" not in plan_ids, "Closed plan should NOT be visible to state_mover"

    def test_state_mover_sees_all_active_item_states(self):
        _seed_realistic_scenario()

        bb = FakeBlackboard()
        node = StateMoverPrepNode(
            name="state_mover_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        active = bb.get_state_value("active_dayflow_items")
        states = {get_meta(i).get("state") for i in active}

        # Should see all non-terminal states
        assert "dispatched" in states
        assert "waiting" in states
        assert "artifact" in states
        # Should NOT see suppressed
        assert "suppressed" not in states


class TestPromptDataCompleteness:
    """Verify no items are silently dropped between DB and prompt."""

    def test_all_plan_tasks_accounted_for(self):
        """Every plan_task in DB should appear either in plan_task_status or active_dayflow_items."""
        _seed_realistic_scenario()

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        # Collect all task IDs from plan_task_status
        pts = bb.get_state_value("plan_task_status")
        plan_task_ids_in_pts = set()
        for plan in pts:
            for t in plan.get("completed_tasks", []) + plan.get("remaining_tasks", []):
                plan_task_ids_in_pts.add(t["task_id"])

        # Collect all item IDs from active_dayflow_items
        active = bb.get_state_value("active_dayflow_items")
        active_ids = {get_meta(i).get("item_id") for i in active}
        active_short_ids = {str(get_meta(i).get("short_id")) for i in active}

        # Load all plan_tasks from DB (excluding suppressed)
        all_items = load_all_items()
        db_plan_tasks = [
            i for i in all_items
            if get_meta(i).get("source_type") == "plan_task"
            and get_meta(i).get("state") != "suppressed"
        ]

        missing = []
        for item in db_plan_tasks:
            meta = get_meta(item)
            item_id = meta["item_id"]
            short_id = str(meta.get("short_id", ""))

            in_pts = (item_id in plan_task_ids_in_pts or
                      short_id in plan_task_ids_in_pts)
            in_active = (item_id in active_ids or
                         short_id in active_short_ids)

            if not in_pts and not in_active:
                missing.append(f"sid:{short_id} id:{item_id} state:{meta.get('state')} plan:{meta.get('plan_id')}")

        assert not missing, \
            f"Plan tasks in DB but not in any prompt section:\n" + "\n".join(missing)
