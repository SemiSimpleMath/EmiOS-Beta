"""Tests for StateMoverPersistNode — reloads state after mutations are persisted."""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> StateMoverPersistNode:
    return StateMoverPersistNode(
        name="state_mover_persist_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


class TestStateMoverPersistNode:

    def test_runs_without_error(self):
        """Persist node runs cleanly after mutations are applied."""
        seed_items([
            make_dayflow_message(item_id="task:reloaded", state="dispatched", short_id=10),
        ])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        # Node no longer writes items to blackboard — it just sets the flag
        assert bb.get_state_value("state_mutations_persisted_tf") is True

    def test_sets_mutations_persisted_flag(self):
        seed_items([make_dayflow_message(item_id="task:dummy", state="actionable")])

        bb = FakeBlackboard()
        node = _make_node(bb)
        node.action_handler(message=None)

        assert bb.get_state_value("state_mutations_persisted_tf") is True

    def test_db_mutation_visible_after_persist(self):
        """If we mutate DB directly, subsequent DB reads reflect the change."""
        seed_items([
            make_dayflow_message(item_id="task:before", state="actionable", short_id=1),
        ])

        # Mutate directly via writer
        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        write_dayflow_item("task:before", state="dispatched", reason="test", caller="test")

        # Verify DB reflects the change
        item = load_item_by_id("task:before")
        assert get_meta(item)["state"] == "dispatched"
