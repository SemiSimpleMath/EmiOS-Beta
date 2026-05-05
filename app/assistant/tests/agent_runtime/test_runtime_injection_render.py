"""Tests for PromptBuilder._append_runtime_injections — the consumer side
of the mailbox runtime-injection feature.

The manager mailbox drain accumulates messages into
``blackboard["_runtime_injections"][agent_name]``. Each agent's prompt
assembly appends its own slot's content to the rendered user prompt, so
the agent sees the injected text on its NEXT activation (and every one
after — chat-style accumulation, never cleared).
"""
from __future__ import annotations

import pytest

from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder


class _FakeBlackboard:
    def __init__(self, state=None):
        self._state = dict(state or {})

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)


class _FakeAgent:
    def __init__(self, *, name, blackboard):
        self.name = name
        self.blackboard = blackboard


@pytest.fixture
def builder():
    return PromptBuilder()


# ── Empty / absent slot → prompt unchanged ────────────────────────


class TestNoInjections:

    def test_no_runtime_key_returns_unchanged(self, builder):
        agent = _FakeAgent(name="planner", blackboard=_FakeBlackboard())
        result = builder._append_runtime_injections(agent, "Original prompt.")
        assert result == "Original prompt."

    def test_no_slot_for_this_agent(self, builder):
        bb = _FakeBlackboard({"_runtime_injections": {"other_agent": ["msg"]}})
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Original prompt.")
        assert result == "Original prompt."

    def test_empty_list(self, builder):
        bb = _FakeBlackboard({"_runtime_injections": {"planner": []}})
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Original prompt.")
        assert result == "Original prompt."

    def test_blank_strings_skipped(self, builder):
        bb = _FakeBlackboard({
            "_runtime_injections": {"planner": ["   ", ""]},
        })
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Original.")
        assert result == "Original."


# ── Injections are appended in posting order ──────────────────────


class TestInjectionsAppended:

    def test_single_injection_appears_at_end(self, builder):
        bb = _FakeBlackboard({
            "_runtime_injections": {"planner": ["+++ steering +++"]},
        })
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Base prompt.")
        assert result.startswith("Base prompt.")
        assert "+++ steering +++" in result
        # Appears after the base, separated by blank lines.
        assert result.index("Base prompt.") < result.index("+++ steering +++")

    def test_multiple_injections_in_order(self, builder):
        bb = _FakeBlackboard({
            "_runtime_injections": {"planner": ["first", "second", "third"]},
        })
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Base.")
        assert result.index("first") < result.index("second") < result.index("third")

    def test_each_agent_sees_only_its_own_slot(self, builder):
        bb = _FakeBlackboard({
            "_runtime_injections": {
                "planner": ["for the planner"],
                "reviewer": ["for the reviewer"],
            },
        })
        planner = _FakeAgent(name="planner", blackboard=bb)
        reviewer = _FakeAgent(name="reviewer", blackboard=bb)
        p_out = builder._append_runtime_injections(planner, "P.")
        r_out = builder._append_runtime_injections(reviewer, "R.")
        assert "for the planner" in p_out
        assert "for the reviewer" not in p_out
        assert "for the reviewer" in r_out
        assert "for the planner" not in r_out


# ── Append-only semantics ─────────────────────────────────────────


class TestAppendOnly:

    def test_render_does_not_mutate_blackboard(self, builder):
        # Critical: rendering must NOT clear the slot. Subsequent agent
        # activations still see the same injections.
        bb = _FakeBlackboard({
            "_runtime_injections": {"planner": ["never goes away"]},
        })
        agent = _FakeAgent(name="planner", blackboard=bb)
        builder._append_runtime_injections(agent, "Base.")
        builder._append_runtime_injections(agent, "Base.")
        store = bb.get_state_value("_runtime_injections")
        assert store == {"planner": ["never goes away"]}


# ── Empty / missing base prompt edge cases ───────────────────────


class TestEdgeCases:

    def test_empty_base_prompt_with_injections(self, builder):
        bb = _FakeBlackboard({
            "_runtime_injections": {"planner": ["the injection"]},
        })
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "")
        assert "the injection" in result

    def test_corrupt_blackboard_value_returns_unchanged(self, builder):
        bb = _FakeBlackboard({"_runtime_injections": "not a dict"})
        agent = _FakeAgent(name="planner", blackboard=bb)
        result = builder._append_runtime_injections(agent, "Base.")
        assert result == "Base."
