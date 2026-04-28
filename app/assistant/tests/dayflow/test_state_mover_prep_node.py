"""Tests for StateMoverPrepNode — refreshes items after planner persistence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    make_chat_message,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> StateMoverPrepNode:
    return StateMoverPrepNode(
        name="state_mover_prep_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


class TestStateMoverPrepNode:

    def test_loads_active_items(self):
        seed_items([
            make_dayflow_message(item_id="task:sm1", state="actionable", summary="Task 1"),
            make_dayflow_message(item_id="task:sm2", state="dispatched", summary="Task 2"),
        ])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        active = bb.get_state_value("active_dayflow_items")
        assert isinstance(active, list)
        assert len(active) >= 2

    def test_excludes_closed_plan_synopses(self):
        seed_items([
            make_plan_synopsis_message(plan_id="P_active", state="active"),
            make_plan_synopsis_message(plan_id="P_closed", state="closed"),
        ])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        synopses = bb.get_state_value("active_plan_synopses")
        plan_ids = [s.get("plan_id") or s.get("plan_id") for s in synopses]
        assert "P_active" in plan_ids
        assert "P_closed" not in plan_ids

    def test_filters_chat_by_cutoff(self):
        now = datetime.now(timezone.utc)
        seed_items([
            make_chat_message(summary="Recent", created_at=now - timedelta(hours=1)),
            make_chat_message(summary="Old", created_at=now - timedelta(hours=8)),
        ])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        chat = bb.get_state_value("recent_dayflow_chat_history")
        summaries = [c["summary"] for c in chat]
        assert "Recent" in summaries
        assert "Old" not in summaries  # Older than 6h cutoff

    def test_excludes_action_logs_from_active(self):
        from app.assistant.tests.dayflow.conftest import make_action_log_message
        now = datetime.now(timezone.utc)
        seed_items([
            make_dayflow_message(item_id="task:real", state="actionable"),
            make_action_log_message(summary="Log entry", created_at=now),
        ])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        active = bb.get_state_value("active_dayflow_items")
        source_types = {get_meta(i).get("source_type") for i in active}
        assert "action_log" not in source_types

    def test_converts_reactivate_at_to_local_in_planned_tasks(self):
        bb = FakeBlackboard({
            "planned_tasks": [{
                "task_id": "t1",
                "reactivate_at": "2026-04-10T17:00:00+00:00",
            }],
        })
        # Seed something so the node doesn't fail on empty DB
        seed_items([make_dayflow_message(item_id="task:dummy", state="actionable")])

        node = _make_node(bb)
        node.action_handler(message=None)

        planned = bb.get_state_value("planned_tasks")
        assert len(planned) == 1
        # Should have reactivate_at_local set
        assert planned[0].get("reactivate_at_local") is not None
        # The original reactivate_at should be preserved (UTC), not overwritten
        assert planned[0].get("reactivate_at") == "2026-04-10T17:00:00+00:00"
