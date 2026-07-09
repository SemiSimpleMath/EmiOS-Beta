"""The chat-nudge pick fires ONCE per turn.

pick_question_for_nudge is side-effecting (marks the question asked,
spends daily ask budget), and prompt assembly can render more than once
per activation — the pick is memoized on the per-invocation blackboard
keyed by the turn's inbound-message anchor (context-injection audit C8).
"""
from __future__ import annotations

from app.assistant.agent_runtime.services.context_injector import ContextInjector
from app.assistant.lib.blackboard.Blackboard import Blackboard


def _agent(anchor):
    bb = Blackboard()
    if anchor:
        bb.update_state_value("inbound_message_id", anchor)

    class _Agent:
        name = "nudge_probe"
        config = {}
        blackboard = bb

    return _Agent()


def test_nudge_pick_memoized_across_repeat_renders(monkeypatch):
    calls = {"n": 0}

    def _fake_pick(*, topic_tag, asked_in_message_id):
        calls["n"] += 1
        return ("q1", "Did you sleep well?")

    import app.assistant.pending_questions as pq
    monkeypatch.setattr(pq, "pick_question_for_nudge", _fake_pick)

    agent = _agent(anchor="msg-row-42")
    injector = ContextInjector()
    ctx1 = injector.generate_injections_block(agent, ["chat_nudges"])
    ctx2 = injector.generate_injections_block(agent, ["chat_nudges"])

    assert ctx1["chat_nudges"] == "Did you sleep well?"
    assert ctx2["chat_nudges"] == "Did you sleep well?"
    assert calls["n"] == 1  # one budget spend per turn, repeat renders reuse it


def test_nudge_empty_pick_also_memoized(monkeypatch):
    calls = {"n": 0}

    def _fake_pick(*, topic_tag, asked_in_message_id):
        calls["n"] += 1
        return None

    import app.assistant.pending_questions as pq
    monkeypatch.setattr(pq, "pick_question_for_nudge", _fake_pick)

    agent = _agent(anchor=None)
    injector = ContextInjector()
    assert injector.generate_injections_block(agent, ["chat_nudges"])["chat_nudges"] == ""
    assert injector.generate_injections_block(agent, ["chat_nudges"])["chat_nudges"] == ""
    assert calls["n"] == 1
