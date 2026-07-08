"""work_node_dispatch guard: a plain dayflow ITEM reaching the work dispatch is closed loudly.

The item lane has no dispatch tail anymore (unification step C pending). Before the guard, a plain
item ref raised inside the dispatch try, could not even be marked failed (no work ref to fail), and
stayed actionable — re-firing the error every pass it was picked.
"""
from __future__ import annotations

from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb):
    return WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                agent_registry={}, tool_registry={})


class TestLegacyItemGuard:

    def test_plain_item_ref_is_closed_and_loop_continues(self):
        seed_items([make_dayflow_message(item_id="task:legacy1", state="actionable", short_id=7)])
        bb = FakeBlackboard({
            "acted_on_item_ids": ["task:legacy1"],
            "delegate_to": "one_shot_tool_runner",
        })
        _make_node(bb).action_handler(message=None)

        item = load_item_by_id("task:legacy1")
        meta = get_meta(item)
        assert meta["state"] == "closed"
        assert meta.get("state_reason") == "legacy_item_lane_dispatch_retired"
        # One dispatch per tick: no loop-back — the state_map routes onward to finalize.
        assert bb.get_state_value("next_agent") is None
        assert bb.get_state_value("acted_on_item_ids") == []

    def test_short_id_ref_resolves_before_close(self):
        seed_items([make_dayflow_message(item_id="task:legacy2", state="actionable", short_id=9)])
        bb = FakeBlackboard({
            "acted_on_item_ids": ["9"],
            "delegate_to": "create_dayflow_ticket",
        })
        _make_node(bb).action_handler(message=None)

        meta = get_meta(load_item_by_id("task:legacy2"))
        assert meta["state"] == "closed"
        assert meta.get("state_reason") == "legacy_item_lane_dispatch_retired"

    def test_empty_ref_no_crash(self):
        bb = FakeBlackboard({"acted_on_item_ids": [], "delegate_to": ""})
        _make_node(bb).action_handler(message=None)
        assert bb.get_state_value("next_agent") is None
