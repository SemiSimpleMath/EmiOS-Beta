"""chat_memory recall is room-scoped — no silent master_room default.

Declaring the chat_memory context item without a room_id on the blackboard
used to fall back to recalling the user's master-room memory into whatever
context happened to lack a room (context-injection audit C7). It is a
config error and refuses loudly now.
"""
from __future__ import annotations

import pytest

from app.assistant.agent_runtime.services.context_injector import ContextInjector


class _BB:
    def __init__(self, room_id=None):
        self._room_id = room_id

    def get_state_value(self, key, default=None):
        if key == "room_id" and self._room_id:
            return self._room_id
        return default


def _agent(room_id=None):
    class _Agent:
        name = "memory_probe"
        config = {}
        blackboard = _BB(room_id)

    return _Agent()


def test_chat_memory_without_room_raises():
    with pytest.raises(ValueError, match="room_id"):
        ContextInjector().generate_injections_block(_agent(room_id=None), ["chat_memory"])


def test_chat_memory_with_room_recalls_scoped(monkeypatch):
    seen = {}

    def _fake_recall(query, *, top_k, room_id):
        seen["room_id"] = room_id
        return [{"text": "past note", "timestamp": "2026-07-01T00:00:00", "type": "summary", "distance": 0.1}]

    import app.assistant.agent_runtime.services.chat_memory_rag as rag
    monkeypatch.setattr(rag, "recall", _fake_recall)

    agent = _agent(room_id="justin")
    agent.blackboard.get_state_value = lambda key, default=None: (
        "justin" if key == "room_id" else ("what did we say?" if key == "task" else default)
    )
    ctx = ContextInjector().generate_injections_block(agent, ["chat_memory"])
    assert seen["room_id"] == "justin"
    assert "past note" in ctx["chat_memory"]
