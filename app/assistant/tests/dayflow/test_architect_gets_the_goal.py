"""The architect is asked to decompose a GOAL, never the tick that happens to be running.

2026-09-16, 22:45 → 2026-09-17, 07:30. work_architect_node was changed to create its agent with
the TICK's blackboard, so that blackboard-sourced context items would resolve instead of reading
empty. The tick's blackboard already carries `task` = "Dayflow cadence tick" (dayflow_tick builds
its Message that way), and the blackboard value wins over the Message the node passes. Every fresh
decomposition for the next eight hours was therefore handed:

    GOAL TO DECOMPOSE:
    Dayflow cadence tick
    CONTEXT: <the entire work portfolio>

The architect did exactly what it was asked. A picture-day goal acquired a node to raise the AC
setpoint, a node to ramp the whole-house lights, and a node to remind about the evening dog walk —
lifted out of the ROUTINE and the portfolio, because those were the only concrete things in view.
Two of them failed inside that goal, and since it is satisfied_when=all_owned_children_done,
preparing a child for picture day became permanently blocked by a thermostat.

The lesson is narrower than "don't share blackboards": an agent's context items resolve from the
blackboard it was built with, so handing it one owned by something else silently rebinds every key
they both use. `task` is the one every agent uses.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/dayflow/test_architect_gets_the_goal.py
"""
from __future__ import annotations

from unittest.mock import patch

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.work_architect_node import WorkArchitectNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard

# What dayflow_tick puts on the tick's blackboard, and what must never reach the architect.
_TICK_TITLE = "Dayflow cadence tick"


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


class _CapturingAgent:
    """Stands in for the architect; records the Message it was actually handed."""

    def __init__(self, sink):
        self._sink = sink

    def action_handler(self, message):
        self._sink["task"] = message.task or ""
        self._sink["information"] = message.information or ""

        class _Result:
            data = {"architect_summary": "no change", "nodes": []}

        return _Result()


def _run_architect_on(bb) -> dict:
    sink: dict = {}
    with patch.object(DI.agent_factory, "create_agent", return_value=_CapturingAgent(sink)):
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
    return sink


def _tick_blackboard(**extra):
    """A blackboard shaped like a real tick's: the tick title already on `task`."""
    base = {"task": _TICK_TITLE, "information": ""}
    base.update(extra)
    return FakeBlackboard(base)


def test_a_fresh_goal_is_decomposed_not_the_tick():
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Prepare for picture day", "goal_content": "Prepare for picture day",
        "satisfied_when_kind": "all_owned_children_done"})

    bb = _tick_blackboard(steward_persist_result={"created": [
        {"work_id": wo.id, "objective": "Prepare the child for makeup Picture Day"},
    ]})
    sink = _run_architect_on(bb)

    assert "Prepare the child for makeup Picture Day" in sink["task"]
    assert _TICK_TITLE not in sink["task"], (
        "the architect was asked to decompose the TICK — it will write a node for everything "
        "in the portfolio it can see")


def test_a_replan_is_given_its_own_goal_not_the_tick():
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Turn off the lights", "goal_content": "Turn off the whole-house lights tonight",
        "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "lights1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Turn off the lights"})

    bb = _tick_blackboard(replan_work_ids=[wo.id])
    sink = _run_architect_on(bb)

    assert "Turn off the whole-house lights tonight" in sink["task"]
    assert _TICK_TITLE not in sink["task"]


def test_the_agent_does_not_share_the_ticks_blackboard():
    """The mechanism, pinned directly: sharing rebinds every key both sides use, and `task` is
    the one every agent uses. Its own blackboard is what keeps the Message authoritative."""
    store = _store()
    wo = store.apply("create_work_object", {
        "title": "Probe", "goal_content": "Probe",
        "satisfied_when_kind": "all_owned_children_done"})

    bb = _tick_blackboard(steward_persist_result={"created": [
        {"work_id": wo.id, "objective": "Probe objective"},
    ]})

    seen_blackboards = []

    def _capture(name, blackboard=None):
        seen_blackboards.append(blackboard)
        return _CapturingAgent({})

    with patch.object(DI.agent_factory, "create_agent", side_effect=_capture):
        WorkArchitectNode(name="work_architect_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)

    assert seen_blackboards, "the architect agent was never created"
    assert all(b is None for b in seen_blackboards), (
        "work_architect_node handed the tick's blackboard to its agent; the tick's `task` will "
        "then shadow the goal")
