"""The architect can end or reword a goal before planning it (2026-10-04).

work_5af7a42b2e83, a heads-up due at 2 PM on 2026-10-02, had no tasks. Every planning tick
`_undecomposed_goals` sent it to the create branch; the architect answered, rightly, end_goal: abandoned
("its useful moment has passed"); the create branch passed only the nodes on, so the goal stayed active and
was asked again ten minutes later, about 40 times on 2026-10-04 alone. Pins: on the create branch an
end_goal ends the work object and its ending is delivered, after which it is no longer decomposed; a
revise_objective rewords the goal.
"""
from __future__ import annotations

from unittest.mock import patch

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.work_architect_node import WorkArchitectNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


class _Architect:
    def __init__(self, data, calls):
        self._data, self._calls = data, calls

    def action_handler(self, message):
        self._calls.append(message.task)
        data = self._data

        class _Result:
            pass

        _Result.data = data
        return _Result()


def _tick(data):
    calls = []
    bb = FakeBlackboard({"task": "Dayflow cadence tick", "information": ""})
    with patch.object(DI.agent_factory, "create_agent", return_value=_Architect(data, calls)), \
            patch("app.assistant.control_nodes.work_architect_node._deliver_ending") as delivered:
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
    return calls, delivered


def test_a_task_less_goal_the_architect_ends_is_ended_and_not_asked_again():
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Heads-up at 2 PM on October 2", "goal_content": "Give the owner a heads-up at 2 PM on October 2",
        "satisfied_when_kind": "all_owned_children_done"})
    end = {"architect_summary": "moot", "nodes": [], "revise_objective": None,
           "end_goal": {"status": "abandoned", "reason": "its useful moment has passed"}}

    calls, delivered = _tick(end)
    assert any("heads-up at 2 PM on October 2" in c for c in calls)
    assert store.load(wo.id).status == "abandoned"
    delivered.assert_any_call(store, wo.id, "abandoned")

    calls, _ = _tick(end)
    assert not any("heads-up at 2 PM on October 2" in c for c in calls), "an ended goal is not decomposed again"


def test_the_create_branch_can_reword_the_goal():
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Remind about the fair", "goal_content": "Remind the owner about the fair on Friday",
        "satisfied_when_kind": "all_owned_children_done"})
    reword = {"architect_summary": "the fair moved", "end_goal": None,
              "revise_objective": {"objective": "Remind the owner about the fair on Saturday",
                                   "reason": "the school moved it"},
              "nodes": [{"node_id": "remind", "title": "Remind about the fair", "detail": "Remind the owner",
                         "depends_on": [], "wake_at": None, "wake_ref": None, "wait_reason": None}]}
    _tick(reword)
    after = store.load(wo.id)
    goal = after.nodes[after.goal_node_id]
    assert "Saturday" in (goal.content or goal.title)
    assert any(after.is_work_unit(n) for n in after.nodes.values())
