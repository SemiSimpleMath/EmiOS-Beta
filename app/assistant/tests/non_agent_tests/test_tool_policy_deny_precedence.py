"""Tool policy: dynamic deny always wins (agent-runtime A2, 2026-07-09).

When no task_allowed_tools was set, get_tools() applied dynamic_denied_tools and
THEN re-added dynamic_allowed_tools — so a tool present in BOTH lists was removed
and then resurrected (deny lost to allow). Deny is now the final filter.
"""
from __future__ import annotations

from app.assistant.agent_runtime.services.tool_policy_resolver import ToolPolicyResolver


class FakeAgentRegistry:
    def __init__(self, config):
        self._config = config

    def get_agent_config(self, name):
        return self._config


class FakeToolRegistry:
    def __init__(self, tools):
        self._tools = list(tools)

    def list_tools(self):
        return list(self._tools)


class FakeBB:
    def __init__(self, state):
        self._state = dict(state)

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)


def _resolver(*, allowed_tools, bb_state):
    reg = FakeAgentRegistry({"allowed_tools": allowed_tools})
    tools = FakeToolRegistry(["read_file", "send_email", "other"])
    bb = FakeBB(bb_state)
    return ToolPolicyResolver(agent_name="a", agent_registry=reg, tool_registry=tools, blackboard=bb)


def test_deny_wins_over_dynamic_allow_with_no_task_allowset():
    # send_email is only dynamically allowed AND dynamically denied — the bug case.
    r = _resolver(
        allowed_tools=["read_file"],
        bb_state={"dynamic_allowed_tools": ["send_email"], "dynamic_denied_tools": ["send_email"]},
    )
    result = r.get_tools()
    assert "send_email" not in result   # deny wins
    assert "read_file" in result


def test_dynamic_allow_still_grants_when_not_denied():
    r = _resolver(
        allowed_tools=["read_file"],
        bb_state={"dynamic_allowed_tools": ["send_email"]},
    )
    assert "send_email" in r.get_tools()   # re-add still works


def test_dynamic_deny_removes_a_statically_allowed_tool():
    r = _resolver(
        allowed_tools=["read_file", "send_email"],
        bb_state={"dynamic_denied_tools": ["send_email"]},
    )
    assert "send_email" not in r.get_tools()


def test_deny_wins_in_the_task_allowset_branch_too():
    # This branch was already correct — guard it against regressions.
    r = _resolver(
        allowed_tools=["read_file", "send_email"],
        bb_state={"task_allowed_tools": ["send_email"], "dynamic_denied_tools": ["send_email"]},
    )
    assert "send_email" not in r.get_tools()
