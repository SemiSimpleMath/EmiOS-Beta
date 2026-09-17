"""work_node_dispatch: everything dispatched is a work node, and nothing else is handled.

Until 2026-09-16 a plain dayflow ITEM could reach this node (the fast-tick lane promoted an
item, the action_selector picked it, the switchboard routed it) and the dispatcher closed it
loudly as a retired-lane stray. That lane is gone — the scheduler no longer arms item timers,
the fast-tick promoter and the item materializer are deleted — so a ref that is not
``work_id::node_id`` is no longer a case to handle. It is a bug, and it RAISES.

There is exactly one path after the switchboard — claim, build arguments, call, judge — and
these states get an exception rather than a bypass route, because a second route would shape
the pipeline around something that cannot happen in a working system, and would not even
cover the case that really strands a node (a raise after the claim).
"""
from __future__ import annotations

import pytest

from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _make_node(bb):
    return WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                agent_registry={}, tool_registry={})


class TestOnlyWorkNodesDispatch:

    def test_non_node_ref_raises_rather_than_routing_around_itself(self):
        """A ref without work_id::node_id cannot be run by this lane, and cannot happen in a
        working system — the selector echoes ids off its own rendered list. So it raises and
        ends the run, instead of earning a bypass route that would make the pipeline
        permanently two-shaped to accommodate a bug."""
        bb = FakeBlackboard({
            "acted_on_item_ids": ["task:legacy1"],
            "delegate_to": "one_shot_tool_runner",
        })
        with pytest.raises(ValueError, match="not a work node"):
            _make_node(bb).action_handler(message=None)
        # The pick is still consumed, so a retry cannot re-fire on the same bad ref.
        assert bb.get_state_value("acted_on_item_ids") == []

    def test_empty_ref_raises(self):
        bb = FakeBlackboard({"acted_on_item_ids": [], "delegate_to": ""})
        with pytest.raises(ValueError, match="not a work node"):
            _make_node(bb).action_handler(message=None)

    def test_a_named_node_with_no_tool_raises_and_fails_the_node(self):
        """delegate_to is required on the switchboard's form, so an empty one means the
        switchboard did not run or did not answer. The node is failed first — otherwise it
        sits ready and is re-picked every pass — and then the run ends loudly."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        wo = store.apply("create_work_object", {"title": "No tool WO", "goal_content": "x",
                                                "satisfied_when_kind": "all_owned_children_done"})
        store.apply("add_node", {"work_id": wo.id, "id": "nt1", "type": "subtask",
                                 "parent_id": wo.goal_node_id, "title": "step"})
        store.apply("set_status", {"work_id": wo.id, "node_id": "nt1", "status": "actionable"})

        bb = FakeBlackboard({"acted_on_item_ids": [f"{wo.id}::nt1"], "delegate_to": ""})
        with pytest.raises(ValueError, match="named no tool"):
            _make_node(bb).action_handler(message=None)
        assert store.load(wo.id).nodes["nt1"].status == "failed"
