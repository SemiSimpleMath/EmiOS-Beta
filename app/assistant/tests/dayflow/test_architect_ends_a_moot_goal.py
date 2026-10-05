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


class _Answers(_Architect):
    """Answers in turn, the last one repeated."""
    def __init__(self, answers, calls):
        super().__init__(None, calls)
        self._answers = list(answers)

    def action_handler(self, message):
        self._data = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return super().action_handler(message)


def _tick_answers(answers):
    calls = []
    bb = FakeBlackboard({"task": "Dayflow cadence tick", "information": ""})
    with patch.object(DI.agent_factory, "create_agent", return_value=_Answers(answers, calls)), \
            patch("app.assistant.control_nodes.work_architect_node._deliver_ending"):
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
    return calls


def test_a_new_goal_left_empty_gets_one_correction_and_is_never_left_empty():
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Set the AC to 70 at 9 PM", "goal_content": "Set the AC to 70 (when: 9:00 PM)",
        "satisfied_when_kind": "all_owned_children_done"})
    empty = {"architect_summary": "the existing set_ac_70 task already does this", "nodes": [],
             "revise_objective": None, "end_goal": None}
    end = {**empty, "end_goal": {"status": "abandoned", "reason": "work_x already sets the AC at 9 PM"}}
    calls = _tick_answers([empty, end])
    assert len(calls) == 2 and "PLANNED NO STEPS AND DID NOT END THE GOAL" in calls[1]
    assert "the existing set_ac_70 task already does this" in calls[1]
    assert store.load(wo.id).status == "abandoned"
    stuck = store.apply("create_work_object", {
        "title": "Remind about the fair", "goal_content": "Remind the owner about the fair",
        "satisfied_when_kind": "all_owned_children_done"})
    calls = _tick_answers([empty])
    assert len(calls) == 2, "one correction, then the error is logged; nothing is written"
    assert store.load(stuck.id).status == "active" and not any(
        store.load(stuck.id).is_work_unit(n) for n in store.load(stuck.id).nodes.values())
