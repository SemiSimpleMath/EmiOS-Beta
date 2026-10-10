"""The architect's answer refused by its form gets one correction (2026-10-10).

The lights-off-after-bedtime goal failed its planning twice that morning: the architect gave its one step both a time
(11:00 PM) and a condition ("after he has gone to bed"), the form refused it (a node waits on a time OR a condition),
the structured-output call failed, and the goal stayed empty to be offered again every tick, with no word to the
architect of what was wrong. Pins: a form refusal is asked once more with the refusal named, and the corrected answer
is applied; a second refusal fails the goal's planning, loudly; a failure that is not the form's is not retried.
Fake agent; the dayflow test store.
"""
from __future__ import annotations

from unittest.mock import patch

from pydantic import BaseModel, ValidationError, model_validator

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.work_architect_node import WorkArchitectNode, _form_refusal
from app.assistant.tests.dayflow.conftest import FakeBlackboard

STEP = {"node_id": "lights_off", "title": "Turn off the lights after bedtime", "detail": "Turn off the lights",
        "depends_on": [], "wake_at": None, "wake_ref": None, "wait_reason": None}
GOOD = {"architect_summary": "one step at 11 PM", "nodes": [STEP], "revise_objective": None, "end_goal": None}


class _Node(BaseModel):
    wake_at: str = ""
    wake_ref: str = ""

    @model_validator(mode="after")
    def _one(self):
        if self.wake_at and self.wake_ref:
            raise ValueError("a node waits on a time (wake_at) OR a condition (wake_ref), not both.")
        return self


def _refused() -> RuntimeError:
    try:
        _Node(wake_at="2026-10-10T23:00:00-07:00", wake_ref="after he has gone to bed")
    except ValidationError as e:
        try:
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
        except RuntimeError as r:
            return r


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
    return calls


def test_a_form_refusal_is_named_back_once_and_the_corrected_answer_is_applied():
    wo = _goal("The household lights are off after the owner has gone to bed")
    calls = _tick([_refused(), GOOD])
    assert len(calls) == 2
    assert "REFUSED" in calls[1] and "not both" in calls[1], "the refusal is named in the correction"
    after = _store().load(wo.id)
    assert [n.title for n in after.nodes.values() if after.is_work_unit(n)] == ["Turn off the lights after bedtime"]


def test_a_second_refusal_leaves_the_goal_unplanned():
    wo = _goal("The porch light is off after midnight")
    calls = _tick([_refused(), _refused()])
    assert len(calls) == 2, "one correction, no more"
    after = _store().load(wo.id)
    assert not any(after.is_work_unit(n) for n in after.nodes.values())


def test_only_the_forms_refusal_is_corrected():
    assert "not both" in _form_refusal(_refused())
    assert _form_refusal(RuntimeError("LLM request failed: connection reset")) is None
