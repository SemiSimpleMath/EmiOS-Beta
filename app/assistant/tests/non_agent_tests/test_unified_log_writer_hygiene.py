"""Unified-log writer hygiene (persistence audit P3+P5, 2026-07-09).

A cross-source ID collision used to log "This should never happen" and
then upsert anyway — rewriting another source's row payload under the
same id. Colliding rows are now refused. And
save_proactive_chat_message rides the db_manager writer queue like the
rest of the module.
"""
from __future__ import annotations

import os

os.environ["USE_TEST_DB"] = "true"
os.environ.setdefault("TEST_DB_NAME", "test_emidb")

import uuid
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.database.db_handler import UnifiedLog2026, initialize_database
from app.assistant.message_manager.save_to_unified_db import (
    save_proactive_chat_message,
    save_to_unified_db,
)
from app.models.base import get_session


def _payload(msg_id: str, source: str, message: str) -> dict:
    return {
        "id": msg_id,
        "timestamp": datetime.now(timezone.utc),
        "role": "assistant",
        "message": message,
        "source": source,
        "metadata_json": {"origin": source},
    }


def _fetch(msg_id: str) -> UnifiedLog2026 | None:
    session = get_session()
    try:
        return session.get(UnifiedLog2026, msg_id)
    finally:
        session.close()


def test_cross_source_collision_is_refused_not_clobbered():
    initialize_database()
    msg_id = f"collision-{uuid.uuid4().hex[:8]}"
    save_to_unified_db([_payload(msg_id, "chat", "original text")], source="chat")

    other_id = f"ok-{uuid.uuid4().hex[:8]}"
    save_to_unified_db(
        [
            _payload(msg_id, "dayflow_item", "attacker text"),
            _payload(other_id, "dayflow_item", "innocent sibling"),
        ],
        source="dayflow_item",
    )

    row = _fetch(msg_id)
    assert row.source == "chat"                    # identity untouched
    assert row.message == "original text"
    assert row.metadata_json == {"origin": "chat"}  # payload NOT clobbered

    assert _fetch(other_id) is not None             # sibling still written


def test_same_source_upsert_still_updates_lifecycle_fields():
    initialize_database()
    msg_id = f"upsert-{uuid.uuid4().hex[:8]}"
    save_to_unified_db([_payload(msg_id, "dayflow_item", "v1")], source="dayflow_item")

    updated = _payload(msg_id, "dayflow_item", "v2-ignored-message")
    updated["metadata_json"] = {"origin": "dayflow_item", "state": "closed"}
    save_to_unified_db([updated], source="dayflow_item")

    row = _fetch(msg_id)
    assert row.message == "v1"                       # identity fields immutable
    assert row.metadata_json["state"] == "closed"    # lifecycle fields update


def test_save_proactive_rides_the_writer_queue(monkeypatch):
    initialize_database()
    ops = []
    from app.models import db_manager as dbm

    real_transaction = dbm.DBManager.transaction

    def _spy(self, *, op="unknown"):
        ops.append(op)
        return real_transaction(self, op=op)

    monkeypatch.setattr(dbm.DBManager, "transaction", _spy)
    msg_id = save_proactive_chat_message(content="proactive hello", sender="assistant")
    assert msg_id
    assert "save_proactive_chat_message" in ops
    assert _fetch(msg_id).message == "proactive hello"
