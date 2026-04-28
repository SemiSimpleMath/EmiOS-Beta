"""Tests for write_dayflow_item — the single source of truth for all dayflow writes."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
    ALLOWED_TRANSITIONS,
    write_dayflow_item,
    write_dayflow_items_batch,
)
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    load_all_items,
    make_dayflow_message,
    seed_items,
)


class TestCreateNewItem:

    def test_creates_item_with_required_fields(self):
        meta = write_dayflow_item(
            "task:new1",
            updates={"source_type": "plan_task", "summary": "New task"},
            state="important_open",
            caller="test",
        )
        assert meta["item_id"] == "task:new1"
        assert meta["state"] == "important_open"
        assert meta["summary"] == "New task"
        assert meta["source_type"] == "plan_task"
        assert meta.get("last_reviewed_at") is not None

        # Verify in DB
        item = load_item_by_id("task:new1")
        assert item is not None
        assert get_meta(item)["state"] == "important_open"

    def test_create_with_content_fallback(self):
        meta = write_dayflow_item(
            "task:new2",
            updates={"source_type": "plan_task"},
            content="Content as summary",
            caller="test",
        )
        assert meta["summary"] == "Content as summary"

    def test_create_missing_source_type_raises(self):
        with pytest.raises(ValueError, match="source_type"):
            write_dayflow_item(
                "task:bad1",
                updates={"summary": "No source type"},
                caller="test",
            )

    def test_create_missing_summary_raises(self):
        with pytest.raises(ValueError, match="summary"):
            write_dayflow_item(
                "task:bad2",
                updates={"source_type": "plan_task"},
                caller="test",
            )

    def test_create_empty_item_id_raises(self):
        with pytest.raises(ValueError, match="item_id"):
            write_dayflow_item("", updates={"source_type": "plan_task", "summary": "X"}, caller="test")


class TestUpdateExistingItem:

    def test_merges_without_clobbering(self):
        # Seed an item
        write_dayflow_item(
            "task:upd1",
            updates={"source_type": "plan_task", "summary": "Original", "importance": "high"},
            state="important_open",
            caller="test_create",
        )

        # Update just one field
        meta = write_dayflow_item(
            "task:upd1",
            updates={"execution_result": "Done successfully"},
            caller="test_update",
        )

        # Original fields preserved
        assert meta["summary"] == "Original"
        assert meta["importance"] == "high"
        assert meta["state"] == "important_open"
        # New field added
        assert meta["execution_result"] == "Done successfully"

    def test_update_stamps_last_reviewed_at(self):
        write_dayflow_item(
            "task:upd2",
            updates={"source_type": "plan_task", "summary": "T"},
            state="important_open",
            caller="test",
        )
        meta = write_dayflow_item(
            "task:upd2",
            updates={"dispatched_at": "2026-04-09T10:00:00Z"},
            caller="test",
        )
        assert meta.get("last_reviewed_at") is not None


class TestStateTransitions:

    def test_valid_transition(self):
        write_dayflow_item(
            "task:tr1",
            updates={"source_type": "plan_task", "summary": "T"},
            state="important_open",
            caller="test",
        )
        meta = write_dayflow_item(
            "task:tr1",
            state="actionable",
            reason="Ready to go",
            caller="test",
        )
        assert meta["state"] == "actionable"

    def test_invalid_transition_raises(self):
        write_dayflow_item(
            "task:tr2",
            updates={"source_type": "plan_task", "summary": "T"},
            state="important_open",
            caller="test",
        )
        with pytest.raises(ValueError, match="invalid transition"):
            write_dayflow_item(
                "task:tr2",
                state="new",  # important_open → new is not allowed
                caller="test",
            )

    def test_closed_to_dispatched_raises(self):
        write_dayflow_item(
            "task:tr3",
            updates={"source_type": "plan_task", "summary": "T"},
            state="actionable",
            caller="test",
        )
        write_dayflow_item("task:tr3", state="closed", caller="test")
        with pytest.raises(ValueError, match="invalid transition"):
            write_dayflow_item("task:tr3", state="dispatched", caller="test")

    def test_suppressed_is_terminal(self):
        write_dayflow_item(
            "task:tr4",
            updates={"source_type": "plan_task", "summary": "T"},
            state="important_open",
            caller="test",
        )
        write_dayflow_item("task:tr4", state="suppressed", caller="test")
        with pytest.raises(ValueError, match="invalid transition"):
            write_dayflow_item("task:tr4", state="actionable", caller="test")

    def test_full_lifecycle(self):
        """important_open → actionable → dispatched → closed"""
        write_dayflow_item(
            "task:lifecycle",
            updates={"source_type": "plan_task", "summary": "Lifecycle test"},
            state="important_open",
            caller="test",
        )
        write_dayflow_item("task:lifecycle", state="actionable", caller="test")
        write_dayflow_item("task:lifecycle", state="dispatched", caller="test")
        meta = write_dayflow_item("task:lifecycle", state="closed", caller="test")
        assert meta["state"] == "closed"

    def test_dispatched_to_closed(self):
        """The critical return path: dispatched → closed."""
        write_dayflow_item(
            "task:dispatch_close",
            updates={"source_type": "plan_task", "summary": "T"},
            state="actionable",
            caller="test",
        )
        write_dayflow_item("task:dispatch_close", state="dispatched", caller="test")
        meta = write_dayflow_item(
            "task:dispatch_close",
            state="closed",
            updates={"execution_result": "All done"},
            reason="dispatch_completed",
            caller="test",
        )
        assert meta["state"] == "closed"
        assert meta["execution_result"] == "All done"

    def test_waiting_to_actionable_reactivation(self):
        write_dayflow_item(
            "task:wait_react",
            updates={"source_type": "plan_task", "summary": "T"},
            state="actionable",
            caller="test",
        )
        write_dayflow_item(
            "task:wait_react",
            state="waiting",
            updates={"wait_reason": "Blocked", "reactivate_at_utc": "2026-04-10T10:00:00Z"},
            caller="test",
        )
        meta = write_dayflow_item(
            "task:wait_react",
            state="actionable",
            reason="Timer fired",
            caller="test",
        )
        assert meta["state"] == "actionable"

    def test_all_transitions_in_map_are_consistent(self):
        """Every target state in ALLOWED_TRANSITIONS should also be a key."""
        all_states = set(ALLOWED_TRANSITIONS.keys())
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                assert target in all_states, \
                    f"Target state '{target}' (from '{source}') is not a key in ALLOWED_TRANSITIONS"


class TestBatchWrite:

    def test_batch_creates_multiple(self):
        items = [
            {"item_id": "task:b1", "source_type": "plan_task", "summary": "Batch 1"},
            {"item_id": "task:b2", "source_type": "plan_task", "summary": "Batch 2"},
            {"item_id": "task:b3", "source_type": "email", "summary": "Batch 3"},
        ]
        count = write_dayflow_items_batch(items, caller="test")
        assert count == 3
        assert load_item_by_id("task:b1") is not None
        assert load_item_by_id("task:b2") is not None
        assert load_item_by_id("task:b3") is not None

    def test_batch_missing_item_id_raises(self):
        with pytest.raises(ValueError, match="item_id"):
            write_dayflow_items_batch([{"source_type": "plan_task", "summary": "X"}], caller="test")

    def test_batch_missing_source_type_raises(self):
        with pytest.raises(ValueError, match="source_type"):
            write_dayflow_items_batch([{"item_id": "task:bad", "summary": "X"}], caller="test")

    def test_batch_upsert(self):
        items = [{"item_id": "task:upsert1", "source_type": "plan_task", "summary": "V1", "state": "important_open"}]
        write_dayflow_items_batch(items, caller="test")

        items[0]["summary"] = "V2"
        write_dayflow_items_batch(items, caller="test")

        item = load_item_by_id("task:upsert1")
        assert get_meta(item)["summary"] == "V2"


class TestDoubleEncodingDefense:

    def test_reads_double_encoded_metadata(self):
        """write_dayflow_item should handle existing double-encoded rows."""
        import json as _json
        from sqlalchemy import insert as sql_insert
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        # Manually insert a double-encoded row
        double_encoded = _json.dumps(_json.dumps({
            "item_id": "task:double", "source_type": "plan_task",
            "state": "dispatched", "summary": "Double encoded task",
        }))
        session = get_session()
        session.execute(
            sql_insert(UnifiedLog2026).values(
                id="task:double",
                timestamp=datetime.now(timezone.utc),
                role="system",
                message="Double encoded task",
                source="dayflow_item",
                room_id="dayflow_orchestrator",
                metadata_json=double_encoded,
                content_type="text",
            )
        )
        session.commit()
        session.close()

        # Update through write_dayflow_item — should read correctly
        meta = write_dayflow_item(
            "task:double",
            state="closed",
            reason="test",
            caller="test",
        )
        assert meta["state"] == "closed"
        assert meta["summary"] == "Double encoded task"
        assert meta["source_type"] == "plan_task"
