"""Tests for state_store.py — the canonical persistence layer for dayflow items."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    load_item_by_id,
    make_action_log_message,
    make_chat_message,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


# ── seed_items / write_dayflow_items_batch ──────────────────────

class TestPersistDayflowItems:

    def test_persist_single_item(self):
        msg = make_dayflow_message(item_id="task:abc", short_id=1, summary="Task A")
        count = seed_items([msg])
        assert count == 1

        item = load_item_by_id("task:abc")
        assert item is not None
        meta = get_meta(item)
        assert meta["item_id"] == "task:abc"
        assert meta["summary"] == "Task A"
        assert meta["short_id"] == 1

    def test_persist_multiple_items(self):
        msgs = [
            make_dayflow_message(item_id=f"task:t{i}", short_id=i, summary=f"Task {i}")
            for i in range(5)
        ]
        count = seed_items(msgs)
        assert count == 5
        assert len(load_all_items()) == 5

    def test_upsert_overwrites_by_item_id(self):
        msg1 = make_dayflow_message(item_id="task:upsert", state="important_open", summary="v1")
        seed_items([msg1])

        msg2 = make_dayflow_message(item_id="task:upsert", state="dispatched", summary="v2")
        seed_items([msg2])

        items = load_all_items()
        # Should be one logical item (upsert by id)
        assert len(items) == 1
        meta = get_meta(items[0])
        assert meta["state"] == "dispatched"
        assert meta["summary"] == "v2"

    def test_persist_plan_synopsis(self):
        msg = make_plan_synopsis_message(plan_id="plan_test_1", synopsis="Do the thing")
        seed_items([msg])

        item = load_item_by_id("plan_synopsis:plan_test_1")
        assert item is not None
        meta = get_meta(item)
        assert meta["source_type"] == "plan_synopsis"
        assert meta["plan_id"] == "plan_test_1"
        assert meta["synopsis"] == "Do the thing"

    def test_persist_chat_message(self):
        msg = make_chat_message(summary="User: hello")
        seed_items([msg])
        items = load_all_items()
        assert len(items) == 1
        assert get_meta(items[0])["source_type"] == "chat"


# ── get_dayflow_items (filtered view) ────────────────────────────

class TestGetDayflowItems:

    def test_excludes_suppressed_items(self):
        seed_items([
            make_dayflow_message(item_id="task:live", state="actionable"),
            make_dayflow_message(item_id="task:dead", state="suppressed"),
        ])
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        items = get_dayflow_items()
        ids = [get_meta(i)["item_id"] for i in items]
        assert "task:live" in ids
        assert "task:dead" not in ids

    def test_excludes_old_closed_items(self):
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        seed_items([
            make_dayflow_message(item_id="task:old_closed", state="closed",
                                created_at=old, last_reviewed_at=old),
            make_dayflow_message(item_id="task:new_closed", state="closed",
                                created_at=recent, last_reviewed_at=recent),
        ])
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        items = get_dayflow_items()
        ids = [get_meta(i)["item_id"] for i in items]
        assert "task:old_closed" not in ids
        assert "task:new_closed" in ids

    def test_includes_active_items(self):
        seed_items([
            make_dayflow_message(item_id="task:open", state="important_open"),
            make_dayflow_message(item_id="task:dispatched", state="dispatched"),
            make_dayflow_message(item_id="task:waiting", state="waiting"),
        ])
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        items = get_dayflow_items()
        ids = [get_meta(i)["item_id"] for i in items]
        assert "task:open" in ids
        assert "task:dispatched" in ids
        assert "task:waiting" in ids


# ── load_existing_dayflow_items ──────────────────────────────────

class TestLoadExistingDayflowItems:

    def test_include_terminal_false_excludes_suppressed(self):
        seed_items([
            make_dayflow_message(item_id="task:ok", state="actionable"),
            make_dayflow_message(item_id="task:gone", state="suppressed"),
        ])
        from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
        items = load_existing_dayflow_items(include_terminal=False)
        ids = [get_meta(i)["item_id"] for i in items]
        assert "task:ok" in ids
        assert "task:gone" not in ids

    def test_include_terminal_true_includes_everything(self):
        seed_items([
            make_dayflow_message(item_id="task:ok", state="actionable"),
            make_dayflow_message(item_id="task:gone", state="suppressed"),
        ])
        from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
        items = load_existing_dayflow_items(include_terminal=True)
        ids = [get_meta(i)["item_id"] for i in items]
        assert "task:ok" in ids
        assert "task:gone" in ids


# ── build_plan_synopsis_messages ─────────────────────────────────

class TestBuildPlanSynopsisMessages:

    def test_builds_correct_messages(self):
        from app.assistant.dayflow_orchestrator.state_store import build_plan_synopsis_messages
        synopses = [{
            "plan_id": "plan_test_build",
            "objective": "Test objective",
            "synopsis": "Test synopsis text",
            "success_criteria": "It works",
            "step_outline": ["step 1", "step 2"],
        }]
        messages = build_plan_synopsis_messages(synopses)
        assert len(messages) == 1
        msg = messages[0]
        assert msg.id == "plan_synopsis:plan_test_build"
        meta = msg.metadata
        assert meta["source_type"] == "plan_synopsis"
        assert meta["plan_id"] == "plan_test_build"
        assert meta["synopsis"] == "Test synopsis text"

    def test_round_trip_persist_and_load(self):
        from app.assistant.dayflow_orchestrator.state_store import build_plan_synopsis_messages
        synopses = [{"plan_id": "plan_rt", "objective": "O", "synopsis": "S"}]
        messages = build_plan_synopsis_messages(synopses)
        seed_items(messages)

        item = load_item_by_id("plan_synopsis:plan_rt")
        assert item is not None
        meta = get_meta(item)
        assert meta["plan_id"] == "plan_rt"
        assert meta["synopsis"] == "S"


# ── scan window (persistence audit P1) ───────────────────────────

def _insert_raw_item(*, item_id: str, state: str, row_ts: datetime,
                     last_reviewed_at: datetime | None = None) -> None:
    """Write a row directly with a controlled ROW timestamp (the batch
    writer always stamps now(), so old rows need a direct insert)."""
    from app.models.base import get_session

    reviewed = (last_reviewed_at or row_ts).isoformat()
    session = get_session()
    try:
        from app.assistant.database.db_handler import UnifiedLog2026 as UL
        session.add(UL(
            id=item_id,
            timestamp=row_ts,
            role="system",
            message=f"raw {item_id}",
            source="dayflow_item",
            room_id="dayflow_orchestrator",
            metadata_json={
                "item_id": item_id,
                "source_type": "plan_task",
                "state": state,
                "summary": f"raw {item_id}",
                "created_at": row_ts.isoformat(),
                "last_reviewed_at": reviewed,
                "data_type": "dayflow_input_item",
            },
        ))
        session.commit()
    finally:
        session.close()


class TestScanWindow:

    def test_old_parked_rows_fall_out_live_rows_stay(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        _insert_raw_item(item_id="task:old_artifact", state="artifact", row_ts=old)
        _insert_raw_item(item_id="task:old_waiting", state="waiting", row_ts=old)

        from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
        ids = [get_meta(i)["item_id"] for i in load_existing_dayflow_items(include_terminal=True)]
        assert "task:old_waiting" in ids       # live state loads regardless of age
        assert "task:old_artifact" not in ids  # parked + untouched → outside the window

    def test_recently_reviewed_old_row_stays(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        _insert_raw_item(
            item_id="task:old_but_touched", state="closed",
            row_ts=old, last_reviewed_at=datetime.now(timezone.utc),
        )
        from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
        ids = [get_meta(i)["item_id"] for i in load_existing_dayflow_items(include_terminal=True)]
        assert "task:old_but_touched" in ids   # every writer stamps last_reviewed_at

    def test_point_lookup_finds_old_artifact(self):
        """A user directive can revive a months-old parked artifact — the
        by-id lookup must skip the scan window entirely."""
        old = datetime.now(timezone.utc) - timedelta(days=90)
        _insert_raw_item(item_id="task:ancient_artifact", state="artifact", row_ts=old)

        item = load_item_by_id("task:ancient_artifact")
        assert item is not None
        assert get_meta(item)["state"] == "artifact"

    def test_point_lookup_respects_exclude_states(self):
        _insert_raw_item(
            item_id="task:suppressed_pt", state="suppressed",
            row_ts=datetime.now(timezone.utc),
        )
        from app.assistant.dayflow_orchestrator.state_store import get_latest_dayflow_item_by_id
        assert get_latest_dayflow_item_by_id("task:suppressed_pt", include_terminal=True) is not None
        assert get_latest_dayflow_item_by_id("task:suppressed_pt", include_terminal=False) is None

    def test_reload_runs_the_scan_once(self, monkeypatch):
        seed_items([make_dayflow_message(item_id="task:once", state="actionable")])

        import app.assistant.dayflow_orchestrator.state_store as ss
        calls = []
        real = ss._load_latest_dayflow_item_map

        def _counting(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(ss, "_load_latest_dayflow_item_map", _counting)
        bb = FakeBlackboard()
        ss.reload_active_items_onto_blackboard(bb, now_utc=datetime.now(timezone.utc), caller="test")
        assert len(calls) == 1  # used to run the full scan twice per reload


# ── reload_active_items_onto_blackboard ──────────────────────────

class TestReloadActiveItemsOntoBlackboard:

    def test_reload_populates_blackboard(self):
        seed_items([
            make_dayflow_message(item_id="task:reload1", state="actionable"),
            make_dayflow_message(item_id="task:reload2", state="dispatched"),
        ])
        bb = FakeBlackboard()
        from app.assistant.dayflow_orchestrator.state_store import reload_active_items_onto_blackboard
        count = reload_active_items_onto_blackboard(bb, now_utc=datetime.now(timezone.utc), caller="test")

        assert count >= 2
        active = bb.get_state_value("active_dayflow_items")
        assert isinstance(active, list)
        assert len(active) >= 2

        existing = bb.get_state_value("existing_dayflow_items")
        assert isinstance(existing, list)
        assert len(existing) >= 2

    def test_reload_excludes_chat_source_type(self):
        seed_items([
            make_dayflow_message(item_id="task:real", state="actionable"),
            make_chat_message(summary="A chat message"),
        ])
        bb = FakeBlackboard()
        from app.assistant.dayflow_orchestrator.state_store import reload_active_items_onto_blackboard
        reload_active_items_onto_blackboard(bb, now_utc=datetime.now(timezone.utc), caller="test")

        active = bb.get_state_value("active_dayflow_items")
        source_types = [get_meta(i).get("source_type") for i in active]
        assert "chat" not in source_types
