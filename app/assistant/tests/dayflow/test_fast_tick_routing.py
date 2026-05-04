"""Tests for the fast-tick routing path.

tick_router_node decides between normal pipeline and fast-tick path based on
the trigger Message's ``fast_tick`` flag + ``triggered_item_id``.
fast_tick_promoter_node deterministically promotes one waiting item to
actionable, and exits cleanly when the item has moved out of waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.fast_tick_promoter_node import FastTickPromoterNode
from app.assistant.control_nodes.tick_router_node import TickRouterNode
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
)


class _FakeMessage:
    def __init__(self, data: dict):
        self.data = data


class TestTickRouter:

    def test_fast_tick_with_item_id_routes_to_promoter(self):
        bb = FakeBlackboard()
        node = TickRouterNode(name="tick_router_node", blackboard=bb,
                              agent_registry={}, tool_registry={})
        node.action_handler(_FakeMessage({
            "fast_tick": True,
            "triggered_item_id": "task:abc",
            "wake_reason": "item_timer",
        }))
        assert bb.get_state_value("next_agent") == "fast_tick_promoter_node"
        assert bb.get_state_value("triggered_item_id") == "task:abc"

    def test_normal_tick_falls_through_default_routing(self):
        bb = FakeBlackboard()
        node = TickRouterNode(name="tick_router_node", blackboard=bb,
                              agent_registry={}, tool_registry={})
        node.action_handler(_FakeMessage({
            "fast_tick": False,
            "wake_reason": "ceiling",
        }))
        # No next_agent override → state_map default applies (intake_triage_prep_node)
        assert bb.get_state_value("next_agent") is None

    def test_fast_tick_without_item_id_falls_through(self):
        """fast_tick=True with no triggered_item_id is a malformed signal — treat as normal."""
        bb = FakeBlackboard()
        node = TickRouterNode(name="tick_router_node", blackboard=bb,
                              agent_registry={}, tool_registry={})
        node.action_handler(_FakeMessage({
            "fast_tick": True,
            "triggered_item_id": "",
            "wake_reason": "item_timer",
        }))
        assert bb.get_state_value("next_agent") is None

    def test_missing_data_field_is_safe(self):
        bb = FakeBlackboard()
        node = TickRouterNode(name="tick_router_node", blackboard=bb,
                              agent_registry={}, tool_registry={})
        node.action_handler(_FakeMessage({}))
        assert bb.get_state_value("next_agent") is None


class TestFastTickPromoter:

    def test_promotes_waiting_item_to_actionable(self):
        # Seed a waiting item directly
        now = datetime.now(timezone.utc)
        write_dayflow_item(
            "task:fastpromo1",
            updates={
                "summary": "Test scheduled action",
                "source_type": "plan_task",
                "reactivate_at_utc": (now - timedelta(seconds=30)).isoformat(),
            },
            state="waiting",
            reason="test_seed",
            caller="test",
        )

        bb = FakeBlackboard({"triggered_item_id": "task:fastpromo1"})
        node = FastTickPromoterNode(name="fast_tick_promoter_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        node.action_handler(message=None)

        item = load_item_by_id("task:fastpromo1")
        assert item is not None
        assert get_meta(item)["state"] == "actionable"
        assert get_meta(item)["state_reason"] == "scheduled_timer_fired"
        # next_agent should NOT be overridden — falls through to view_materializer
        assert bb.get_state_value("next_agent") is None

    def test_exits_cleanly_when_item_no_longer_waiting(self):
        """If the item moved out of waiting (e.g. user closed it), the promoter
        should refuse the transition and exit, not crash."""
        now = datetime.now(timezone.utc)
        write_dayflow_item(
            "task:fastpromo2",
            updates={
                "summary": "Already closed",
                "source_type": "plan_task",
            },
            state="waiting",
            reason="test_seed",
            caller="test",
        )
        # User closes it before the timer worker runs
        write_dayflow_item(
            "task:fastpromo2",
            state="closed",
            reason="user_closed",
            caller="test",
        )

        bb = FakeBlackboard({"triggered_item_id": "task:fastpromo2"})
        node = FastTickPromoterNode(name="fast_tick_promoter_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        node.action_handler(message=None)

        # Item stays closed
        item = load_item_by_id("task:fastpromo2")
        assert get_meta(item)["state"] == "closed"
        # Promoter routes to manager_exit_node — no dispatch this tick
        assert bb.get_state_value("next_agent") == "manager_exit_node"

    def test_exits_cleanly_when_no_triggered_item_id(self):
        bb = FakeBlackboard()  # no triggered_item_id
        node = FastTickPromoterNode(name="fast_tick_promoter_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        node.action_handler(message=None)
        assert bb.get_state_value("next_agent") == "manager_exit_node"
