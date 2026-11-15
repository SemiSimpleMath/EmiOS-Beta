"""The architect's answer refused by its form fails the goal's planning, after the agent's own one correction.

The lights-off-after-bedtime goal failed its planning twice on 2026-10-10: the architect gave its one step both a time
and a condition, the form refused it, and the goal stayed empty with no word to the architect of what was wrong. From
2026-10-10 the node re-asked on the form's refusal; since 2026-11-15 every agent's model call corrects a form refusal
once (agent_runtime/correction.py, its own tests in agent_tests/test_agent_output_correction.py) and raises
AgentOutputInvalid when still refused. Pins: the node asks the agent once, and a still-refused answer leaves the goal
unplanned and the tick running. Fake agent; the dayflow test store.
"""
from __future__ import annotations

from unittest.mock import patch

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_runtime.correction import AgentOutputInvalid, Problem
from app.assistant.control_nodes.work_architect_node import WorkArchitectNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard

STEP = {"node_id": "lights_off", "title": "Turn off the lights after bedtime", "detail": "Turn off the lights",
        "depends_on": [], "wake_at": None, "wake_ref": None, "wait_reason": None}
GOOD = {"architect_summary": "one step at 11 PM", "nodes": [STEP], "revise_objective": None, "end_goal": None}


class _Architect:
    """Answers in turn: an exception is raised, a dict is the answer."""

    def __init__(self, answers, calls):
        self._answers, self._calls = list(answers), calls

    def action_handler(self, message):
        self._calls.append(message.task)
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer

        class _Result:
            data = answer
        return _Result()


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _goal(title):
    return _store().apply("create_work_object", {"title": title, "goal_content": title,
                                                  "satisfied_when_kind": "all_owned_children_done"})


def _tick(answers):
    calls = []
    bb = FakeBlackboard({"task": "Dayflow cadence tick", "information": ""})
    with patch.object(DI.agent_factory, "create_agent", return_value=_Architect(answers, calls)), \
            patch("app.assistant.control_nodes.work_architect_node._deliver_ending"):
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
    return calls, bb


def test_an_accepted_answer_is_asked_once_and_applied():
    wo = _goal("The household lights are off after the owner has gone to bed")
    calls, _ = _tick([GOOD])
    assert len(calls) == 1
    after = _store().load(wo.id)
    assert [n.title for n in after.nodes.values() if after.is_work_unit(n)] == ["Turn off the lights after bedtime"]


def test_a_still_refused_answer_is_not_asked_again_and_leaves_the_goal_unplanned():
    wo = _goal("The porch light is off after midnight")
    refused = AgentOutputInvalid("work_architect", [Problem(why="a node waits on a time OR a condition, not both",
                                                            field="nodes", at="0")])
    calls, bb = _tick([refused, GOOD])
    assert len(calls) == 1, "the agent's own call made the one correction"
    after = _store().load(wo.id)
    assert not any(after.is_work_unit(n) for n in after.nodes.values())
    assert bb.get_state_value("work_decompose_result") == []
