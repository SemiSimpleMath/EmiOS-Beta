"""Room-summary metadata write-back (2026-07-27, the compactor collision loop).

The compactor used to re-save original room_ui rows under
source='room_summary::<room>' to record its suppressed/pinned/processed
marks. The cross-source collision guard (correctly) refused every such
write, so the marks never persisted: each pass re-summarized the same
messages and minted duplicate summary rows into room history (610 refused
writes and six copies of one summary on 2026-07-25).

Guards:
- update_unified_log_metadata changes ONLY metadata_json on the existing
  row (identity fields untouched), never inserts, and reports a miss;
- _set_meta_and_persist lands its marks through that path;
- the cross-source collision guard itself still refuses foreign-source
  re-saves (the fix went around it, not through it).
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_room_summary_metadata_writeback")

import uuid
from datetime import datetime, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.message_manager.save_to_unified_db import (
    save_to_unified_db,
    update_unified_log_metadata,
)
from app.assistant.room_session_manager.services.room_chat_summary import (
    _set_meta_and_persist,
)
from app.assistant.utils.pydantic_classes import Message
from app.models.base import Base, get_session


ROOM = "master_room"


@pytest.fixture(autouse=True)
def _tables():
    session = get_session()
    try:
        Base.metadata.create_all(session.bind)
        session.query(UnifiedLog2026).delete()
        session.commit()
    finally:
        session.close()


def _seed_room_ui_row(message="hello there") -> str:
    row_id = str(uuid.uuid4())
    save_to_unified_db(
        [{
            "id": row_id,
            "timestamp": datetime.now(timezone.utc),
            "role": "user",
            "message": message,
            "source": "room_ui",
            "room_id": ROOM,
            "metadata_json": {"origin": "seed"},
        }],
        source="room_ui",
    )
    return row_id


def _load(row_id: str) -> UnifiedLog2026:
    session = get_session()
    try:
        row = session.query(UnifiedLog2026).filter(UnifiedLog2026.id == row_id).one()
        session.expunge(row)
        return row
    finally:
        session.close()


def _count() -> int:
    session = get_session()
    try:
        return session.query(UnifiedLog2026).count()
    finally:
        session.close()


def test_metadata_update_touches_only_metadata():
    row_id = _seed_room_ui_row()
    ok = update_unified_log_metadata(row_id, {"origin": "seed", "context_suppressed_by_room": {ROOM: True}})
    assert ok is True
    row = _load(row_id)
    assert row.metadata_json["context_suppressed_by_room"] == {ROOM: True}
    assert row.source == "room_ui"
    assert row.message == "hello there"


def test_metadata_update_miss_reports_and_never_inserts():
    _seed_room_ui_row()
    before = _count()
    ok = update_unified_log_metadata(str(uuid.uuid4()), {"x": 1})
    assert ok is False
    assert _count() == before


def test_set_meta_and_persist_lands_marks_on_the_original_row():
    row_id = _seed_room_ui_row()
    m = Message(id=row_id, content="hello there", room_id=ROOM, metadata={"origin": "seed"})
    _set_meta_and_persist(m, datetime.now(timezone.utc), room_id=ROOM, suppressed=True)
    row = _load(row_id)
    meta = row.metadata_json or {}
    assert meta.get("context_suppressed_by_room", {}).get(ROOM) is True
    assert meta.get("room_summary_processed_at_by_room", {}).get(ROOM)
    assert row.source == "room_ui"  # identity untouched — no cross-source rewrite


def test_cross_source_collision_guard_still_refuses():
    row_id = _seed_room_ui_row()
    save_to_unified_db(
        [{
            "id": row_id,
            "timestamp": datetime.now(timezone.utc),
            "role": "assistant",
            "message": "OVERWRITE ATTEMPT",
            "source": f"room_summary::{ROOM}",
            "room_id": ROOM,
            "metadata_json": {"clobbered": True},
        }],
        source=f"room_summary::{ROOM}",
    )
    row = _load(row_id)
    assert row.message == "hello there"
    assert row.source == "room_ui"
    assert "clobbered" not in (row.metadata_json or {})
