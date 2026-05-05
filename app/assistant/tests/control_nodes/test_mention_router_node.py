"""Tests for MentionRouterNode: @name parsing, resolution, inbox posting,
and short-circuit routing."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.assistant.control_nodes.mention_router_node import MentionRouterNode
from app.assistant.utils.pydantic_classes import Message


class _FakeBlackboard:
    def __init__(self, state=None):
        self._state = dict(state or {})

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)

    def update_state_value(self, key, value):
        self._state[key] = value


@pytest.fixture
def node_factory():
    """Factory that returns (node, mock_inbox, captured_publishes)."""
    def _make(*, active_workers=None, initial_blackboard=None):
        bb = _FakeBlackboard(initial_blackboard or {"task": ""})
        node = MentionRouterNode(
            name="mention_router_node",
            blackboard=bb,
            agent_registry=MagicMock(),
            tool_registry=MagicMock(),
        )

        captured = []
        def fake_publish(*, sender, text):
            captured.append((sender, text))
        node._publish_ack = fake_publish

        mock_inbox = MagicMock()
        mock_inbox.post.return_value = True

        return node, bb, mock_inbox, captured, active_workers or []

    return _make


def _patch_di_and_resolvers(active_workers, mock_inbox):
    """Returns a context manager that patches the DI + helpers used by the node."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "app.assistant.control_nodes.mention_router_node.DI"
    )).steering_inbox = mock_inbox

    if active_workers:
        worker_lookup = {
            w["display_name"].lower(): w for w in active_workers
        }
    else:
        worker_lookup = {}

    def fake_find(name):
        return worker_lookup.get((name or "").lower())

    def fake_list():
        return [w["display_name"] for w in active_workers]

    stack.enter_context(patch(
        "app.assistant.control_nodes.mention_router_node.find_active_invocation_by_display_name",
        side_effect=fake_find,
    ))
    stack.enter_context(patch(
        "app.assistant.control_nodes.mention_router_node.list_active_display_names",
        side_effect=fake_list,
    ))
    return stack


# ── No-mention path ─────────────────────────────────────────────────


class TestNoMention:

    def test_plain_text_falls_through(self, node_factory):
        node, bb, mock_inbox, captured, workers = node_factory()
        msg = Message(content="just a regular message")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        # No short-circuit set: next_agent should be None (state_map default
        # routes onward).
        assert bb.get_state_value("next_agent") is None
        assert captured == []

    def test_empty_message_falls_through(self, node_factory):
        node, bb, mock_inbox, captured, workers = node_factory()
        msg = Message(content="")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        assert bb.get_state_value("next_agent") is None

    def test_unknown_at_name_falls_through(self, node_factory):
        # @steve isn't in the registry — let chat_gate handle the message.
        node, bb, mock_inbox, captured, workers = node_factory()
        msg = Message(content="@steve please help")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        assert bb.get_state_value("next_agent") is None
        assert captured == []


# ── @mention without active worker ─────────────────────────────────


class TestNoActiveWorker:

    def test_quimby_not_active_acks_and_short_circuits(self, node_factory):
        node, bb, mock_inbox, captured, workers = node_factory(
            active_workers=[],  # nothing is running
        )
        msg = Message(content="@quimby check savory")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        assert bb.get_state_value("next_agent") == "final_answer_node"
        assert len(captured) == 1
        sender, text = captured[0]
        assert sender == "Em"
        assert "No active Quimby" in text
        # Inbox not touched.
        mock_inbox.post.assert_not_called()

    def test_no_active_includes_other_active_workers_hint(self, node_factory):
        active = [{"display_name": "Phyllis", "invocation_id": "inv-phy",
                   "manager_name": "personal_admin_manager"}]
        node, bb, mock_inbox, captured, workers = node_factory(active_workers=active)
        msg = Message(content="@quimby help")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        sender, text = captured[0]
        assert "active right now: Phyllis" in text


# ── @mention with active worker ────────────────────────────────────


class TestActiveMention:

    def test_routes_to_inbox_and_acks(self, node_factory):
        active = [{"display_name": "Quimby", "invocation_id": "inv-quimby-123",
                   "manager_name": "web_manager"}]
        node, bb, mock_inbox, captured, workers = node_factory(active_workers=active)
        msg = Message(content="@quimby check the savory section")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        # Inbox got the body (text after the @mention).
        mock_inbox.post.assert_called_once_with(
            invocation_id="inv-quimby-123",
            text="check the savory section",
        )
        # Short-circuited.
        assert bb.get_state_value("next_agent") == "final_answer_node"
        # Acked from worker's name.
        sender, text = captured[0]
        assert sender == "Quimby"
        assert "Quimby noted" in text

    def test_case_insensitive_mention(self, node_factory):
        active = [{"display_name": "Quimby", "invocation_id": "inv-q",
                   "manager_name": "web_manager"}]
        node, bb, mock_inbox, captured, workers = node_factory(active_workers=active)
        msg = Message(content="@QUIMBY uppercase test")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        mock_inbox.post.assert_called_once()
        assert bb.get_state_value("next_agent") == "final_answer_node"

    def test_empty_body_acks_without_posting(self, node_factory):
        active = [{"display_name": "Quimby", "invocation_id": "inv-q",
                   "manager_name": "web_manager"}]
        node, bb, mock_inbox, captured, workers = node_factory(active_workers=active)
        msg = Message(content="@quimby")
        with _patch_di_and_resolvers(workers, mock_inbox):
            node.action_handler(msg)
        mock_inbox.post.assert_not_called()
        assert bb.get_state_value("next_agent") == "final_answer_node"
        sender, text = captured[0]
        assert "didn't say anything" in text
