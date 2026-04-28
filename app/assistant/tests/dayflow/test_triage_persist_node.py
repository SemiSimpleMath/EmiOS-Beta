"""Tests for TriagePersistNode — persists admitted artifacts to DB."""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.triage_persist_node import TriagePersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> TriagePersistNode:
    return TriagePersistNode(
        name="triage_persist_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


class TestTriagePersistNode:

    def test_persists_admitted_artifacts(self):
        """Admitted artifacts should be written to DB with last_reviewed_at set."""
        artifact = {
            "id": "email:test1",
            "metadata": {
                "item_id": "email:test1",
                "source_type": "email",
                "event_type": "email_received",
                "state": "artifact",
                "summary": "New email from boss",
                "importance": "high",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        bb = FakeBlackboard({
            "admitted_artifacts": [artifact],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("email:test1")
        assert item is not None
        meta = get_meta(item)
        assert meta["state"] == "artifact"
        assert meta["summary"] == "New email from boss"
        assert meta.get("last_reviewed_at") is not None

    def test_empty_admitted_artifacts_is_noop(self):
        bb = FakeBlackboard({"admitted_artifacts": []})
        node = _make_node(bb)
        node.action_handler(message=None)
        assert len(load_all_items()) == 0

    def test_persisted_items_visible_in_db(self):
        """After persisting, items should be readable from DB."""
        artifact = {
            "id": "cal:ev1",
            "metadata": {
                "item_id": "cal:ev1",
                "source_type": "calendar",
                "event_type": "calendar_event",
                "state": "artifact",
                "summary": "Meeting at 3pm",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        bb = FakeBlackboard({"admitted_artifacts": [artifact]})
        node = _make_node(bb)
        node.action_handler(message=None)

        # Item should be in DB (not checking blackboard — prep nodes load fresh).
        item = load_item_by_id("cal:ev1")
        assert item is not None
        assert get_meta(item)["summary"] == "Meeting at 3pm"
