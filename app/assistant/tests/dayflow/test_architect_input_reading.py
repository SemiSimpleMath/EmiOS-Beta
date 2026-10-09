"""What the architect reads about other work (2026-10-08 read of a ~96k-character re-plan prompt).

- RECENTLY COMPLETED showed objectives cut mid-word ("befo", "electricit": the stored work-object
  title is the objective cut at 80 characters) and no dates. Each entry now carries the goal's full
  objective and when it ended, in local time.
- The goal being re-planned was printed twice in full: as its existing graph in the task, and again
  inside the portfolio. The portfolio now only marks it.

Run:
    .venv\\Scripts\\python.exe -m pytest app/assistant/tests/dayflow/test_architect_input_reading.py
"""
from __future__ import annotations

from unittest.mock import patch

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.strategic_planner_wo_prep_node import StrategicPlannerWoPrepNode
from app.assistant.control_nodes.work_architect_node import WorkArchitectNode
from app.assistant.dayflow_orchestrator.work_portfolio import render_active_portfolio
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.utils.time_utils import local_time_text

LONG_OBJECTIVE = ("Remind the user to submit the quarterly electricity rebate form before the utility "
                  "closes its application window on Friday, and confirm the confirmation number arrived")


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _goal_with_task(store, objective, task_title):
    wo = store.apply("create_work_object", {"title": objective[:80], "goal_content": objective,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": f"{wo.id}_step", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": task_title})
    return wo


def _replan(bb) -> dict:
    sink: dict = {}

    class _Agent:
        def action_handler(self, message):
            sink["task"], sink["information"] = message.task or "", message.information or ""

            class _Result:
                data = {"architect_summary": "no change", "nodes": []}
            return _Result()

    with patch.object(DI.agent_factory, "create_agent", return_value=_Agent()):
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
    return sink


def test_a_long_completed_objective_reaches_the_architect_whole_with_its_end_time():
    store = _store()
    ended = _goal_with_task(store, LONG_OBJECTIVE, "Send the rebate reminder")
    store.apply("set_work_status", {"work_id": ended.id, "status": "done", "reason": "form submitted"},
                actor="architect")
    ended_at = store.load(ended.id).nodes[ended.goal_node_id].payload["terminal"]["at"]
    current = _goal_with_task(store, "Water the garden tonight", "Tell the user to water the garden")

    prep = StrategicPlannerWoPrepNode(name="prep", blackboard=FakeBlackboard({}),
                                      agent_registry={}, tool_registry={})
    completed, _ = prep._render_recent_completed(store.list_work_objects(), store)
    sink = _replan(FakeBlackboard({"replan_work_ids": [current.id], "recent_completed_work": completed}))

    assert LONG_OBJECTIVE in sink["information"]
    assert f"(completed {local_time_text(ended_at)})" in sink["information"]


def test_the_goals_own_work_object_appears_once():
    store = _store()
    current = _goal_with_task(store, "Water the garden tonight", "Tell the user to water the garden")
    other = _goal_with_task(store, "Book the dentist appointment", "Ask the user which day suits")
    tick_portfolio, _, _ = render_active_portfolio(store, store.list_work_objects())

    sink = _replan(FakeBlackboard({"replan_work_ids": [current.id], "work_portfolio": tick_portfolio}))
    prompt = sink["task"] + sink["information"]

    assert prompt.count("GOAL: Tell the user to water the garden") == 1, "the goal's own graph was printed twice"
    assert "(this goal — its full work graph is shown above)" in sink["information"]
    assert "Ask the user which day suits" in sink["information"], "other work must stay in the portfolio"
    assert "=== WORK OBJECT W1 ===" in sink["information"], "other work by its label of the call (2026-10-08)"
    assert other.id not in prompt and current.id not in prompt, "no work id reaches the architect"
