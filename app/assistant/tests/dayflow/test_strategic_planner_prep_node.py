"""Tests for StrategicPlannerPrepNode — the pre-LLM node that builds planner context."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.strategic_planner_prep_node import (
    StrategicPlannerPrepNode,
    _build_plan_task_status,
    _build_recent_changes,
    _build_recent_dispatch_results,
)
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


# ── _build_plan_task_status ──────────────────────────────────────

class TestBuildPlanTaskStatus:

    def test_groups_tasks_by_plan(self):
        items = [
            {"metadata": {"source_type": "plan_task", "plan_id": "P1", "short_id": 1,
                          "item_id": "t1", "state": "dispatched", "summary": "Task 1"}},
            {"metadata": {"source_type": "plan_task", "plan_id": "P1", "short_id": 2,
                          "item_id": "t2", "state": "closed", "summary": "Task 2"}},
            {"metadata": {"source_type": "plan_task", "plan_id": "P2", "short_id": 3,
                          "item_id": "t3", "state": "waiting", "summary": "Task 3",
                          "wait_reason": "Blocked", "reactivate_at": "2026-04-10"}},
        ]
        synopses = [
            {"plan_id": "P1", "state": "active", "synopsis": "Plan 1", "objective": "O1"},
            {"plan_id": "P2", "state": "active", "synopsis": "Plan 2", "objective": "O2"},
        ]
        result = _build_plan_task_status(items, synopses)
        assert len(result) == 2

        p1 = next(r for r in result if r["plan_id"] == "P1")
        assert len(p1["completed_tasks"]) == 1  # closed
        assert len(p1["remaining_tasks"]) == 1  # dispatched
        assert p1["plan_state"] == "active"

        p2 = next(r for r in result if r["plan_id"] == "P2")
        assert len(p2["remaining_tasks"]) == 1
        wait_task = p2["remaining_tasks"][0]
        assert wait_task["state"] == "waiting"
        assert wait_task.get("wait_reason") == "Blocked"

    def test_closed_plan_state(self):
        items = [
            {"metadata": {"source_type": "plan_task", "plan_id": "P_done", "short_id": 1,
                          "item_id": "t1", "state": "closed", "summary": "Done task"}},
        ]
        synopses = [
            {"plan_id": "P_done", "state": "closed", "synopsis": "Finished plan"},
        ]
        result = _build_plan_task_status(items, synopses)
        assert len(result) == 1
        assert result[0]["plan_state"] == "closed"
        assert len(result[0]["completed_tasks"]) == 1
        assert len(result[0]["remaining_tasks"]) == 0

    def test_plan_with_no_tasks(self):
        items = []
        synopses = [
            {"plan_id": "P_empty", "state": "active", "synopsis": "Empty plan"},
        ]
        result = _build_plan_task_status(items, synopses)
        assert len(result) == 1
        assert result[0]["completed_tasks"] == []
        assert result[0]["remaining_tasks"] == []

    def test_ignores_non_plan_items(self):
        items = [
            {"metadata": {"source_type": "email", "plan_id": "", "item_id": "e1",
                          "state": "artifact", "summary": "Email"}},
            {"metadata": {"source_type": "plan_task", "plan_id": "P1", "short_id": 1,
                          "item_id": "t1", "state": "actionable", "summary": "Real task"}},
        ]
        synopses = [{"plan_id": "P1", "state": "active", "synopsis": "Plan"}]
        result = _build_plan_task_status(items, synopses)
        assert len(result) == 1
        assert len(result[0]["remaining_tasks"]) == 1

    def test_execution_result_included(self):
        items = [
            {"metadata": {"source_type": "plan_task", "plan_id": "P1", "short_id": 1,
                          "item_id": "t1", "state": "dispatched", "summary": "Task",
                          "execution_result": "Did the thing successfully"}},
        ]
        synopses = [{"plan_id": "P1", "state": "active", "synopsis": "Plan"}]
        result = _build_plan_task_status(items, synopses)
        task = result[0]["remaining_tasks"][0]
        assert task.get("execution_result") == "Did the thing successfully"

    def test_cadence_tick_execution_result_filtered(self):
        items = [
            {"metadata": {"source_type": "plan_task", "plan_id": "P1", "short_id": 1,
                          "item_id": "t1", "state": "dispatched", "summary": "Task",
                          "execution_result": "Inbound UI chat message from system: cadence tick"}},
        ]
        synopses = [{"plan_id": "P1", "state": "active", "synopsis": "Plan"}]
        result = _build_plan_task_status(items, synopses)
        task = result[0]["remaining_tasks"][0]
        assert not task.get("execution_result")


# ── _build_recent_dispatch_results ───────────────────────────────

class TestRecentDispatchResultsPods:
    """A dispatched manager that minted research findings stamps pod_references
    onto the plan step; the planner-facing dispatch record must carry them so
    the planner references the pods instead of re-dispatching the research.
    """

    def _item(self, now_utc, *, pod_references):
        meta = {
            "source_type": "plan_task",
            "item_id": "t1",
            "short_id": 1,
            "state": "closed",
            "summary": "Research Irvine HVAC providers",
            "dispatched_to": "emi_team_manager",
            "dispatched_at": now_utc.isoformat(),
            "execution_result": "Found 2 HVAC providers in Irvine.",
        }
        if pod_references is not None:
            meta["pod_references"] = pod_references
        return {"metadata": meta}

    def test_pod_references_threaded_into_record(self):
        now = datetime.now(timezone.utc)
        pods = [{"pod_id": "datapod:research_finding:3519561be7b1", "one_liner": "Acme Air — IAQ"}]
        out = _build_recent_dispatch_results([self._item(now, pod_references=pods)], now)
        assert len(out) == 1
        assert out[0]["pod_references"] == pods

    def test_no_pods_yields_empty_list(self):
        now = datetime.now(timezone.utc)
        out = _build_recent_dispatch_results([self._item(now, pod_references=None)], now)
        assert len(out) == 1
        assert out[0]["pod_references"] == []


# ── _build_recent_changes ────────────────────────────────────────

class TestBuildRecentChanges:

    def test_returns_empty_when_no_watermark(self):
        result = _build_recent_changes([], since_utc=None, now_utc=datetime.now(timezone.utc))
        assert result == []

    def test_includes_new_items_since_watermark(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "plan_task", "state": "actionable",
                "short_id": 42, "summary": "New task",
                "created_at": (now - timedelta(minutes=5)).isoformat(),
                "last_reviewed_at": None,
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 1
        assert "task 42" in result[0]
        assert "actionable" in result[0]

    def test_excludes_items_before_watermark(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "plan_task", "state": "actionable",
                "short_id": 1, "summary": "Old task",
                "created_at": (now - timedelta(hours=2)).isoformat(),
                "last_reviewed_at": (now - timedelta(hours=1)).isoformat(),
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 0

    def test_includes_chat_messages(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "chat", "state": "artifact",
                "summary": "User said hello",
                "created_at": (now - timedelta(minutes=3)).isoformat(),
                "last_reviewed_at": None,
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 1
        assert "User said:" in result[0]

    def test_excludes_action_dispatch(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "action_dispatch", "state": "artifact",
                "summary": "Dispatch log",
                "created_at": (now - timedelta(minutes=3)).isoformat(),
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 0

    def test_excludes_suppressed(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "plan_task", "state": "suppressed",
                "short_id": 1, "summary": "Suppressed task",
                "created_at": (now - timedelta(minutes=3)).isoformat(),
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 0

    def test_plan_synopsis_format(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "plan_synopsis", "state": "active",
                "plan_id": "P1", "plan_status": "active",
                "summary": "New plan for taxes",
                "created_at": (now - timedelta(minutes=3)).isoformat(),
            }},
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 1
        assert "Plan [P1]" in result[0]

    def test_caps_at_25(self):
        now = datetime.now(timezone.utc)
        watermark = now - timedelta(minutes=10)
        items = [
            {"metadata": {
                "source_type": "plan_task", "state": "actionable",
                "short_id": i, "summary": f"Task {i}",
                "created_at": (now - timedelta(minutes=5)).isoformat(),
            }}
            for i in range(30)
        ]
        result = _build_recent_changes(items, since_utc=watermark, now_utc=now)
        assert len(result) == 25


# ── Full prep node integration ───────────────────────────────────

class TestStrategicPlannerPrepNodeIntegration:

    def test_prep_node_populates_all_blackboard_keys(self):
        """Seed DB with various items and verify the prep node produces expected keys."""
        now = datetime.now(timezone.utc)
        seed_items([
            make_dayflow_message(item_id="task:t1", short_id=1, state="actionable",
                                summary="Active task", plan_id="P1"),
            make_dayflow_message(item_id="task:t2", short_id=2, state="closed",
                                summary="Done task", plan_id="P1",
                                source_type="plan_task"),
            make_plan_synopsis_message(plan_id="P1", synopsis="Test plan"),
            make_chat_message(summary="User chat", created_at=now - timedelta(minutes=5)),
            make_action_log_message(summary="Dispatched X", created_at=now - timedelta(minutes=5)),
        ])

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        # Core keys must be populated
        assert bb.get_state_value("active_dayflow_items") is not None
        assert isinstance(bb.get_state_value("active_dayflow_items"), list)
        assert bb.get_state_value("plan_task_status") is not None
        assert isinstance(bb.get_state_value("plan_task_status"), list)
        assert bb.get_state_value("recent_dayflow_chat_history") is not None
        assert bb.get_state_value("recent_changes") is not None
        assert bb.get_state_value("admitted_artifacts") is not None

    def test_prep_node_separates_source_types(self):
        """Chat and action_log items should NOT appear in active_dayflow_items."""
        now = datetime.now(timezone.utc)
        seed_items([
            make_dayflow_message(item_id="task:real", state="actionable", summary="Real"),
            make_chat_message(summary="Chat item", created_at=now),
            make_action_log_message(summary="Log item", created_at=now),
        ])

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        active = bb.get_state_value("active_dayflow_items")
        source_types = {get_meta(i).get("source_type") for i in active}
        assert "chat" not in source_types
        assert "action_log" not in source_types
        assert "action_dispatch" not in source_types

    def test_prep_node_builds_chat_history(self):
        now = datetime.now(timezone.utc)
        seed_items([
            make_chat_message(summary="Recent chat", created_at=now - timedelta(minutes=30)),
            make_chat_message(summary="Old chat", created_at=now - timedelta(hours=15)),
        ])

        bb = FakeBlackboard({"admitted_artifacts": []})
        node = StrategicPlannerPrepNode(
            name="strategic_planner_prep_node",
            blackboard=bb,
            agent_registry={},
            tool_registry={},
        )
        node.action_handler(message=None)

        chat = bb.get_state_value("recent_dayflow_chat_history")
        assert len(chat) == 1  # Only the recent one (within 12h)
        assert "Recent chat" in chat[0]["summary"]

    def test_prep_node_plan_task_status_groups_correctly(self):
        seed_items([
            make_dayflow_message(
                item_id="task:pt1", short_id=10, state="dispatched",
                summary="Task in plan", plan_id="P1", source_type="plan_task",
            ),
            make_dayflow_message(
                item_id="task:pt2", short_id=11, state="closed",
                summary="Closed task in plan", plan_id="P1", source_type="plan_task",
            ),
            make_plan_synopsis_message(plan_id="P1", synopsis="Plan 1"),
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
        assert len(pts) == 1
        p1 = pts[0]
        assert p1["plan_id"] == "P1"
        assert len(p1["completed_tasks"]) == 1
        assert len(p1["remaining_tasks"]) == 1
