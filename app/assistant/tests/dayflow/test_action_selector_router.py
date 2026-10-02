"""action_selector_router_node: the selector picks; the switchboard gets the node as the architect wrote it.

2026-10-02: the selector restated the Seija send node as a question for the owner, the switchboard routed
the restatement to a ticket, and the approved email went out only on the retry. The selector now returns
an id and nothing else, and this router stages the picked node's own record.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.assistant.control_nodes.action_selector_router_node import ActionSelectorRouterNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard

SELECTOR = "dayflow_orchestrator::action_selector"


def _router(bb):
    return ActionSelectorRouterNode(name="action_selector_router_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})


def _node(title, content):
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    store = get_dayflow_work_store()
    wo = store.apply("create_work_object", {"title": "Seija link", "goal_content": "Seija has the link",
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "send_link", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": title, "content": content})
    return f"{wo.id}::send_link"


def test_the_switchboard_gets_the_nodes_own_record():
    ref = _node("Send Seija the approved Zoom link",
                "Email Seija exactly the draft the owner approved in approve_seija_email.")
    bb = FakeBlackboard({"last_agent": SELECTOR, "acted_on_item_ids": [ref],
                         "actionable_items": [{"item_id": ref}]})
    _router(bb).action_handler(message=None)
    assert bb.get_state_value("task") == "Send Seija the approved Zoom link"
    assert bb.get_state_value("information") == "Email Seija exactly the draft the owner approved in approve_seija_email."
    assert bb.get_state_value("acted_on_item_ids") == [ref]
    assert bb.get_state_value("next_agent") == "dayflow_orchestrator::switchboard"


def test_a_decorated_echo_is_staged_under_the_canonical_id():
    ref = _node("Step", "Do the step.")
    bb = FakeBlackboard({"last_agent": SELECTOR, "acted_on_item_ids": [f"task:{ref}"],
                         "actionable_items": [{"item_id": ref}]})
    _router(bb).action_handler(message=None)
    assert bb.get_state_value("acted_on_item_ids") == [ref]


def test_no_op_ends_the_pass():
    bb = FakeBlackboard({"last_agent": SELECTOR, "no_op_tf": True, "acted_on_item_ids": []})
    _router(bb).action_handler(message=None)
    assert bb.get_state_value("next_agent") == "final_answer_node"
    assert bb.get_state_value("result")["final_answer_no_op"] is True


@pytest.mark.parametrize("picked", [[], ["a::1", "b::2"]])
def test_anything_but_one_pick_raises(picked):
    bb = FakeBlackboard({"last_agent": SELECTOR, "acted_on_item_ids": picked})
    with pytest.raises(ValueError, match="exactly one"):
        _router(bb).action_handler(message=None)


def test_a_pick_the_store_does_not_have_raises():
    bb = FakeBlackboard({"last_agent": SELECTOR, "acted_on_item_ids": ["work_missing::n1"],
                         "actionable_items": [{"item_id": "work_missing::n1"}]})
    with pytest.raises(Exception):
        _router(bb).action_handler(message=None)


def test_the_selector_form_carries_a_pick_and_nothing_to_rewrite():
    from app.assistant.agents.dayflow_orchestrator.action_selector.agent_form import AgentForm
    assert set(AgentForm.model_fields) == {"reasoning", "acted_on_item_ids", "no_op_tf"}
    AgentForm(reasoning="r", acted_on_item_ids=["w::n"])
    AgentForm(reasoning="r", no_op_tf=True)
    with pytest.raises(ValidationError):
        AgentForm(reasoning="r", acted_on_item_ids=[])
    with pytest.raises(ValidationError):
        AgentForm(reasoning="r", no_op_tf=True, acted_on_item_ids=["w::n"])
