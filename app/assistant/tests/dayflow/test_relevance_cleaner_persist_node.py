"""Tests for RelevanceCleanerPersistNode — applies close/suppress decisions."""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.relevance_cleaner_persist_node import RelevanceCleanerPersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> RelevanceCleanerPersistNode:
    return RelevanceCleanerPersistNode(
        name="relevance_cleaner_persist_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


class TestRelevanceCleanerPersistNode:

    def test_close_decision_closes_item(self):
        seed_items([
            make_dayflow_message(item_id="task:stale1", short_id=50, state="dispatched",
                                summary="Stale dispatched task"),
        ])

        bb = FakeBlackboard({
            "decisions": [
                {"item_id": "50", "action": "close", "reason": "No longer relevant"},
            ],
            "short_id_mapping": {"50": "task:stale1"},
            "existing_dayflow_items": [
                {"metadata": {"item_id": "task:stale1", "short_id": 50, "state": "dispatched"}},
            ],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:stale1")
        meta = get_meta(item)
        assert meta["state"] == "closed"

    def test_suppress_decision_suppresses_item(self):
        seed_items([
            make_dayflow_message(item_id="task:noisy", short_id=51, state="artifact",
                                summary="Noisy item"),
        ])

        bb = FakeBlackboard({
            "decisions": [
                {"item_id": "51", "action": "suppress", "reason": "Spam"},
            ],
            "short_id_mapping": {"51": "task:noisy"},
            "existing_dayflow_items": [
                {"metadata": {"item_id": "task:noisy", "short_id": 51, "state": "artifact"}},
            ],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:noisy")
        meta = get_meta(item)
        assert meta["state"] == "suppressed"

    def test_empty_decisions_is_noop(self):
        seed_items([
            make_dayflow_message(item_id="task:safe", short_id=52, state="actionable"),
        ])

        bb = FakeBlackboard({
            "decisions": [],
            "short_id_mapping": {},
            "existing_dayflow_items": [],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:safe")
        meta = get_meta(item)
        assert meta["state"] == "actionable"  # Unchanged

    def test_resolves_short_id_to_canonical(self):
        """The LLM gives short_ids; persist node must resolve to canonical item_ids."""
        seed_items([
            make_dayflow_message(item_id="task:canonical1", short_id=60, state="watching"),
        ])

        bb = FakeBlackboard({
            "decisions": [
                {"item_id": "60", "action": "close", "reason": "Done watching"},
            ],
            "short_id_mapping": {"60": "task:canonical1"},
            "existing_dayflow_items": [
                {"metadata": {"item_id": "task:canonical1", "short_id": 60, "state": "watching"}},
            ],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:canonical1")
        meta = get_meta(item)
        assert meta["state"] == "closed"
