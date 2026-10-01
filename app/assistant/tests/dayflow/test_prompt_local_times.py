"""Times shown to dayflow agents are local, never raw UTC; tool-call receipts carry their time."""
from zoneinfo import ZoneInfo

import pytest

from app.assistant.dayflow_orchestrator.work_context import render_view, worker_data
from app.assistant.utils import time_utils
from work_objects.store import WorkStore


@pytest.fixture(autouse=True)
def pacific(monkeypatch):
    monkeypatch.setattr(time_utils, "get_local_timezone", lambda: ZoneInfo("America/Los_Angeles"))


def test_local_time_text():
    assert time_utils.local_time_text("2026-09-30T14:00:33.5+00:00") == "Wed 09-30 07:00 AM"
    assert time_utils.local_time_text("2026-09-30T14:00:33.5+00:00", seconds=True) == "Wed 09-30 07:00:33 AM"
    assert time_utils.local_time_text(None) == "" and time_utils.local_time_text("") == ""
    with pytest.raises(ValueError):
        time_utils.local_time_text("not a time")


def test_finalizer_receipts_show_each_call_local_time():
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", dict(title="t", goal_content="t", satisfied_when_kind="all_owned_children_done"))
    store.apply("add_node", dict(work_id=wo.id, id="main", type="subtask", parent_id=wo.goal_node_id, title="main"))
    wo = store.load(wo.id)
    view = worker_data(wo, "main")
    view["work"]["execution"] = {"recent_results": [
        {"node_id": "main", "epoch": 1, "tool_name": "devices", "state": "settled",
         "detail": "applied", "updated_at": "2026-09-30T14:00:40+00:00"},
        {"node_id": "main", "epoch": 1, "tool_name": "devices", "state": "settled",
         "detail": "target 74", "updated_at": "2026-09-30T14:00:33+00:00"}]}
    text = render_view("finalizer_input", view=view, result_text="done", repeat_failure_limit=2)
    assert "[settled] at Wed 09-30 07:00:40 AM: applied" in text
    assert "[settled] at Wed 09-30 07:00:33 AM: target 74" in text
    assert "14:00" not in text


def test_architect_instructions_and_concern_history_are_local():
    text = render_view("architect_task", mode="replan", objective="o", finalizer_block="", graph="",
                       goal_instructions=[{"at": "2026-09-30T17:20:00+00:00", "reason": "moot"}], stranded=False)
    assert "- Wed 09-30 10:20 AM: moot" in text
    concerns = [{"concern_id": "c1234567", "title": "t", "status": "addressing", "done_when": "d", "owner_words": "", "brief": None,
                 "attempts": [{"work_id": "w", "objective": "o", "status": "abandoned",
                               "attached_at": "2026-09-30T07:39:00+00:00",
                               "ended": {"outcome": "abandoned", "at": "2026-09-30T14:20:00+00:00", "reason": "r"},
                               "judgments": [{"title": "set", "verdict": "retry", "outcome": "x",
                                              "at": "2026-09-30T14:01:03+00:00", "replies": []}]}]}]
    text = render_view("concern_brief", concerns=concerns)
    assert "attached Wed 09-30 12:39 AM" in text
    assert "ended abandoned Wed 09-30 07:20 AM" in text
    assert "judged retry (Wed 09-30 07:01 AM)" in text


def test_boot_validator_parses_templates_that_use_the_filter():
    from types import SimpleNamespace
    from app.assistant.validation import agent_validator
    registry = SimpleNamespace(configs={"probe": {"prompts": {"user": "{{ at | local_time }}"},
                                                  "user_context_items": ["at"]}})
    agent_validator._check_undeclared_template_vars(registry)
