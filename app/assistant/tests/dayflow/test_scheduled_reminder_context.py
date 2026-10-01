"""Communication evidence remains distinct from delivery and task completion."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import yaml

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.dayflow_orchestrator import communication_context as context
from app.assistant.dayflow_orchestrator.work_context import render_view
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.models.base import get_session

NOW = datetime(2026, 9, 21, 18, tzinfo=timezone.utc)
TEXT = "Reminder: prepare the meeting notes. " + "Preserve all details. " * 40


def seed(record_id, **overrides):
    values = dict(id=record_id, timestamp=NOW - timedelta(minutes=10),
                  source="scheduler_reminder", room_id="master_room",
                  room_surface="ui", room_context_id="main", direction="outbound", message=TEXT)
    values.update(overrides)
    session = get_session()
    try:
        session.add(UnifiedLog2026(**values))
        session.commit()
    finally:
        session.close()


def test_history_is_full_ordered_scoped_and_windowed():
    seed("recent-b")
    seed("recent-a")  # Same wording is not a semantic duplicate decision.
    seed("boundary", timestamp=NOW - timedelta(hours=18))
    seed("old", timestamp=NOW - timedelta(hours=18, microseconds=1))
    seed("future", timestamp=NOW + timedelta(seconds=1))
    seed("other-room", room_id="another_room")
    seed("incoming", direction="inbound")
    seed("other-source", source="dayflow_item")
    result = context.scheduled_reminder_history(NOW)
    assert result["status"] == "available"
    assert [r["id"] for r in result["records"]] == ["boundary", "recent-a", "recent-b"]
    for row in result["records"]:
        assert row["text"] == TEXT
        assert row["delivery_status"] == row["read_status"] == "unknown"
    rendered = render_view("scheduled_reminders", scheduled_reminder_history=result)
    assert TEXT in rendered
    assert "BEFORE delivery" in rendered
    assert "overlap may be appropriate" in rendered
    assert "completed task" in rendered


def test_failed_read_cannot_masquerade_as_empty_history(monkeypatch):
    monkeypatch.setattr(context, "get_db_manager", Mock(side_effect=RuntimeError("read failed")))
    result = context.scheduled_reminder_history(NOW)
    assert result["status"] == "unavailable"
    rendered = render_view("scheduled_reminders", scheduled_reminder_history=result)
    assert "History unavailable" in rendered
    assert "No matching records" not in rendered


def test_empty_and_missing_history_are_distinct():
    empty = render_view("scheduled_reminders", scheduled_reminder_history=context.scheduled_reminder_history(NOW))
    missing = render_view("scheduled_reminders")
    assert "No matching records in this window" in empty
    assert "History unavailable" in missing
    with pytest.raises(ValueError):
        context.scheduled_reminder_history(NOW.replace(tzinfo=None))


def test_all_three_agent_views_render_the_same_full_record():
    seed("source-proof")
    history = context.scheduled_reminder_history(NOW)
    from app.assistant.utils.path_utils import get_repo_root
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env as env
    root = get_repo_root() / "app/assistant/agents"
    for agent in ("state_mover", "strategic_planner_wo"):
        base = "dayflow_orchestrator/" + agent
        config = yaml.safe_load((root / base / "config.yaml").read_text(encoding="utf-8"))
        assert "scheduled_reminder_history" in config["user_context_items"]
        data = {key: None for key in config["user_context_items"]}
        data["scheduled_reminder_history"] = history
        text = env.get_template(base + "/prompts/user.j2").render(**data)
        assert TEXT in text and "source-proof" in text
    from app.assistant.control_nodes.work_architect_node import _situational_context
    rendered = _situational_context(FakeBlackboard({"scheduled_reminder_history": history}))
    assert TEXT in rendered and "source-proof" in rendered


@pytest.mark.parametrize("path", ["steward", "tick", "wake"])
def test_prep_refreshes_history_for_each_entry_path(monkeypatch, path):
    from app.assistant.control_nodes import state_mover_prep_node as state
    from app.assistant.control_nodes import strategic_planner_wo_prep_node as steward
    from app.assistant.control_nodes.work_node_wake_prep_node import WorkNodeWakePrepNode
    from app.assistant.dayflow_orchestrator import blackboard_builder
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    fresh = {"status": "available", "records": [], "since": "start", "through": "end"}
    load = Mock(return_value=fresh)
    monkeypatch.setattr(context, "scheduled_reminder_history", load)
    monkeypatch.setattr(state, "recent_user_context", lambda *args: ([], {}))
    monkeypatch.setattr(steward.StrategicPlannerWoPrepNode, "_build_situational_context", lambda self: None)
    monkeypatch.setattr(blackboard_builder, "build_dayflow_blackboard_extras", lambda: {})
    bb = FakeBlackboard({"scheduled_reminder_history": {"status": "stale"}})
    if path == "wake":
        store = get_dayflow_work_store()
        wo = store.apply("create_work_object", {"title": "Prepare notes", "goal_content": "Prepare notes"})
        store.apply("add_node", {"work_id": wo.id, "id": "notes", "type": "subtask",
                                "parent_id": wo.goal_node_id, "title": "Prepare notes"})
        bb.update_state_value("triggered_work_node", wo.id + "::notes")
        cls = WorkNodeWakePrepNode
    else:
        cls = state.StateMoverPrepNode if path == "tick" else steward.StrategicPlannerWoPrepNode
    cls(name="prep", blackboard=bb, agent_registry={}, tool_registry={}).action_handler(None)
    assert bb.get_state_value("scheduled_reminder_history") == fresh
    load.assert_called_once()
    assert load.call_args.args[0].tzinfo is not None
