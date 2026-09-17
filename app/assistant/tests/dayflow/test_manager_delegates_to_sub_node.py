"""A working manager delegating to another work manager — the common pattern.

work_emi_team is given a top-level node and hands pieces of it to other work managers
(work_web_manager and friends). Each delegation gets its own sub-node so the graph reads
`goal -> the step -> the web bit`, and so a later pass can see what was already tried.

THIS PATH WAS DEAD FOR SIX WEEKS AND NO TEST NOTICED. The child is created and run in one
breath — nothing can race it, so nothing claims it, so it sits at `proposed`. On 2026-08-04
the shared runner gained an assertion refusing any node that was not `dispatched`, which is
correct for a DISPATCHED node (the gate claims those before calling) and fatal here: every
delegation raised on the node it had just created. Exactly one node-handoff succeeded after
that date, in June, before the assertion existed.

So these tests pin the two halves of it: a sub-node needs no claim, and its outcome lands on
the sub-node as provenance.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/dayflow/test_manager_delegates_to_sub_node.py
"""
from __future__ import annotations

import pytest

from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

# work_web_manager is `node_aware` — the config that makes a manager run ON graph nodes.
_DELEGATE = "work_web_manager"


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _parent_node(store, title="Delegating WO"):
    """A top-level node mid-call, i.e. what work_emi_team is running on when it delegates."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "parent1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Do the whole thing"})
    for st in ("actionable", "dispatched"):
        store.apply("set_status", {"work_id": wo.id, "node_id": "parent1", "status": st})
    return wo.id, "parent1"


@pytest.fixture
def delegating(monkeypatch):
    """Inside a work run on parent1, with the sub-manager's own call stubbed out."""
    from work_objects.runtime import reset_work_context, set_work_context

    store = _store()
    work_id, node_id = _parent_node(store)

    calls = []

    def _fake_invoke_on(self, *, task, information, scope_context, **kw):
        calls.append({"task": task, "information": information})
        return ToolResult(result_type="success",
                          content="found the opening hours: 9 to 5 on weekdays", data={})

    monkeypatch.setattr(ManagerInterface, "invoke_on", _fake_invoke_on)

    token = set_work_context(store, work_id, node_id, actor="work_emi_team_manager")
    try:
        yield store, work_id, node_id, calls
    finally:
        reset_work_context(token)


def _delegate(question="find the opening hours"):
    return ManagerInterface(_DELEGATE).execute(ToolMessage(
        tool_name=_DELEGATE,
        tool_data={"arguments": {"task": question, "information": ""}},
    ))


def test_delegation_creates_a_sub_node_and_runs_it(delegating):
    """The whole pattern in one assertion set: a child appears under the caller's node, the
    sub-manager actually runs, and the caller gets its ToolResult back."""
    store, work_id, node_id, calls = delegating

    before = set(store.load(work_id).nodes)
    result = _delegate()
    after = store.load(work_id).nodes

    # Two nodes appear: the sub-node, and the evidence child carrying its result.
    new_subtasks = [after[i] for i in set(after) - before if after[i].type == "subtask"]
    assert len(new_subtasks) == 1, "exactly one sub-node per delegation"
    child = new_subtasks[0]

    assert calls, "the sub-manager was never invoked — the delegation did not happen"
    assert "opening hours" in (result.content or "")
    assert child.type == "subtask"
    assert child.owner_agent == _DELEGATE


def test_a_sub_node_needs_no_claim(delegating):
    """The regression that killed this path. A sub-node is created and run in one breath with
    nothing able to race it, so requiring `dispatched` refuses the node just created."""
    store, work_id, node_id, calls = delegating

    _delegate()          # must not raise
    assert calls, "delegation raised before reaching the sub-manager"


def test_the_sub_nodes_outcome_is_recorded_on_it(delegating):
    """Provenance: a later pass reads what was already tried rather than redoing it."""
    store, work_id, node_id, calls = delegating

    before = set(store.load(work_id).nodes)
    _delegate()
    wo = store.load(work_id)
    # Two nodes appear — the sub-node and its evidence child. Pick by type, not by set order.
    child_id = next(i for i in set(wo.nodes) - before if wo.nodes[i].type == "subtask")

    assert wo.nodes[child_id].status in ("done", "closed"), "the sub-node kept no outcome"
    evidence = [n for n in wo.nodes.values()
                if n.parent_id == child_id and n.type in ("evidence", "artifact")]
    assert evidence, "no evidence recorded under the sub-node"
    assert "opening hours" in " ".join((e.content or "") for e in evidence)


def test_the_parent_is_untouched_by_the_delegation(delegating):
    """The sub-team must not take over the node its caller is working on."""
    store, work_id, node_id, calls = delegating

    _delegate()
    assert store.load(work_id).nodes[node_id].status == "dispatched"


def test_outside_a_work_run_it_is_an_ordinary_sub_manager_call(monkeypatch):
    """No work context -> no node is invented; it behaves like any other manager tool."""
    store = _store()
    work_id, _ = _parent_node(store, title="Untouched WO")
    before = set(store.load(work_id).nodes)

    monkeypatch.setattr(ManagerInterface, "invoke_on",
                        lambda self, **kw: ToolResult(result_type="success",
                                                      content="plain answer", data={}))
    result = _delegate()

    assert result.content == "plain answer"
    assert set(store.load(work_id).nodes) == before, "a node was created with no work context"
