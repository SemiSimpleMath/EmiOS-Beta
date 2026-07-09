"""Blackboard core (Blackboard audit fixes B1-B4, 2026-07-09).

B1: request_id had a split source of truth — the manager wrote the
instance attribute (add_request_id) while _tool_caller_util and the
approval gateway read the scope key (get_state_value), which nothing
wrote, so those readers always got None. Now both converge.
B3: results/history/tool_results were write-only dead attrs.
B4: __init__ and reset_blackboard seeded different key sets.
"""
from __future__ import annotations

from app.assistant.lib.blackboard.Blackboard import Blackboard


class TestRequestIdUnified:
    def test_add_request_id_populates_both_reader_styles(self):
        bb = Blackboard()
        bb.add_request_id("req-123")
        # The attribute reader (get_request_id) and the scope-key reader
        # (get_state_value) now return the same id.
        assert bb.get_request_id() == "req-123"
        assert bb.get_state_value("request_id") == "req-123"

    def test_scope_key_no_longer_stuck_at_none(self):
        bb = Blackboard()
        # Before the fix this was always None regardless of add_request_id.
        assert bb.get_state_value("request_id") is None   # unset baseline
        bb.add_request_id("req-abc")
        assert bb.get_state_value("request_id") == "req-abc"

    def test_request_id_visible_through_a_pushed_child_scope(self):
        """request_id is written to the GLOBAL scope, so a child scope
        (pushed for a sub-agent call) still resolves it via fall-through —
        which is exactly what _tool_caller_util reads inside a call."""
        bb = Blackboard()
        bb.add_request_id("req-xyz")
        bb.push_call_context("planner", "worker", "scope_1")
        assert bb.get_state_value("request_id") == "req-xyz"
        bb.pop_call_context()
        assert bb.get_state_value("request_id") == "req-xyz"


class TestDeadAttrsGone:
    def test_no_write_only_dead_attributes(self):
        bb = Blackboard()
        for attr in ("results", "history", "tool_results"):
            assert not hasattr(bb, attr), f"dead attr {attr!r} still present"
        bb.reset_blackboard()
        for attr in ("results", "history", "tool_results"):
            assert not hasattr(bb, attr), f"reset re-created dead attr {attr!r}"
        # The live message log stays.
        assert hasattr(bb, "messages")


class TestSeedParity:
    def test_fresh_and_reset_seed_the_same_keys(self):
        fresh = Blackboard()
        reset = Blackboard()
        reset.reset_blackboard()
        # Every key a reset blackboard exposes, a fresh one exposes too
        # (same default) — so get_state_value("checklist") etc. behave the
        # same whether or not reset ran.
        assert set(fresh.scopes[0].keys()) == set(reset.scopes[0].keys())
        for key in reset.scopes[0]:
            assert fresh.get_state_value(key) == reset.get_state_value(key), key

    def test_list_defaults_are_iterable_on_a_fresh_blackboard(self):
        bb = Blackboard()
        for key in ("checklist", "summary", "discovered_info", "progress"):
            val = bb.get_state_value(key)
            assert isinstance(val, list), f"{key} should seed to [], got {val!r}"


class TestScopeStack:
    def test_local_shadows_then_falls_through_to_global(self):
        bb = Blackboard()
        bb.update_global_state_value("k", "global")
        bb.push_call_context("a", "b", "s1")
        assert bb.get_state_value("k") == "global"      # fall-through
        bb.update_state_value("k", "local")
        assert bb.get_state_value("k") == "local"        # shadow
        bb.pop_call_context()
        assert bb.get_state_value("k") == "global"       # shadow gone with the scope

    def test_history_ids_are_monotonic(self):
        from app.assistant.utils.pydantic_classes import Message

        bb = Blackboard()
        bb.add_msg(Message(content="a"))
        bb.add_msg(Message(content="b"))
        ids = [m.metadata["history_id"] for m in bb.get_messages()]
        assert ids == [1, 2]
