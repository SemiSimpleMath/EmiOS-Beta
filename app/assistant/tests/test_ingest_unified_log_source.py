"""Tests for ingest.sources.unified_log_source.UnifiedLogSource.

Uses the test_setup harness and the real unified_log_2026 table. Each
test tags its rows with a known source value so cleanup can be targeted;
the fixture wipes those test-owned rows before and after each run.
"""
import app.assistant.tests.test_setup  # noqa: F401

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.ingest.sources.unified_log_source import (
    UnifiedLogSource,
    _CURSOR_KEY,
)
from app.models.base import get_session


# Test-owned source tag. Any row written under this source is ours and safe
# to delete between tests. Using a unique-enough name so we don't collide
# with real operational rows.
_TEST_SOURCE = "unified_log_source_test_fixture"


def _clear_test_rows() -> None:
    session = get_session()
    try:
        session.query(UnifiedLog2026).filter_by(source=_TEST_SOURCE).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _insert_row(
    *,
    room_id: str,
    message: str = "hello",
    speaker_name: str = "test_user",
    direction: str = "inbound",
    data_type_hint: str = "chat",
    msg_id: str | None = None,
) -> str:
    """Insert one unified_log row tagged with our test source. Returns its id."""
    row_id = msg_id or f"test-{uuid.uuid4()}"
    session = get_session()
    try:
        row = UnifiedLog2026(
            id=row_id,
            timestamp=datetime.now(timezone.utc),
            role="user",
            message=message,
            source=_TEST_SOURCE,
            processed=False,
            room_id=room_id,
            direction=direction,
            speaker_name=speaker_name,
            metadata_json={"data_type_hint": data_type_hint},
        )
        session.add(row)
        session.commit()
        return row_id
    finally:
        session.close()


def _current_max_rowid() -> int:
    """Return the current max rowid in unified_log_2026 (any source)."""
    from sqlalchemy import text
    session = get_session()
    try:
        result = session.execute(
            text("SELECT MAX(rowid) FROM unified_log_2026")
        ).scalar()
        return int(result) if result is not None else 0
    finally:
        session.close()


def _wind_cursor_to_current_max() -> None:
    """Pin the cursor to the current max rowid so subsequent inserts are 'new'.

    This lets each test start with a clean 'nothing new yet' state even
    though unified_log_2026 is a shared table with real operational data.
    """
    max_rid = _current_max_rowid()
    IngestCursorStore().set(source_key=_CURSOR_KEY, cursor_value=str(max_rid))


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_test_rows()
    IngestCursorStore().clear(_CURSOR_KEY)
    yield
    _clear_test_rows()
    IngestCursorStore().clear(_CURSOR_KEY)


def test_pull_returns_envelopes_for_master_room_messages():
    _wind_cursor_to_current_max()
    id1 = _insert_row(room_id="master_room", message="first")
    id2 = _insert_row(room_id="master_room", message="second")

    envelopes = UnifiedLogSource().pull()

    assert len(envelopes) == 2
    assert [e.content for e in envelopes] == ["first", "second"]
    assert [e.signal_id for e in envelopes] == [id1, id2]
    assert all(e.source_type == "unified_log" for e in envelopes)
    assert all(e.metadata["room_id"] == "master_room" for e in envelopes)


def test_pull_includes_slack_and_telegram_rooms_by_prefix():
    _wind_cursor_to_current_max()
    _insert_row(room_id="slack/C12345", message="slack_msg")
    _insert_row(room_id="tg_9876", message="telegram_msg_tg")
    _insert_row(room_id="telegram/123", message="telegram_msg_prefix")

    envelopes = UnifiedLogSource().pull()

    assert len(envelopes) == 3
    room_ids = {e.metadata["room_id"] for e in envelopes}
    assert room_ids == {"slack/C12345", "tg_9876", "telegram/123"}


def test_pull_excludes_dayflow_orchestrator_rows():
    _wind_cursor_to_current_max()
    _insert_row(room_id="dayflow_orchestrator", message="internal")
    _insert_row(room_id="master_room", message="user_msg")

    envelopes = UnifiedLogSource().pull()

    assert len(envelopes) == 1
    assert envelopes[0].content == "user_msg"


def test_pull_excludes_internal_rooms():
    _wind_cursor_to_current_max()
    _insert_row(room_id="task_create", message="task")
    _insert_row(room_id="doc_editor", message="doc")
    _insert_row(room_id="task_spec::spammer_finder", message="spec")
    _insert_row(room_id="master_room", message="real")

    envelopes = UnifiedLogSource().pull()

    assert len(envelopes) == 1
    assert envelopes[0].content == "real"


def test_second_pull_returns_empty_when_no_new_rows():
    _wind_cursor_to_current_max()
    _insert_row(room_id="master_room", message="one")

    source = UnifiedLogSource()
    first = source.pull()
    second = source.pull()

    assert len(first) == 1
    assert second == []


def test_pull_only_returns_new_rows_after_cursor():
    _wind_cursor_to_current_max()
    _insert_row(room_id="master_room", message="old_a")
    _insert_row(room_id="master_room", message="old_b")

    source = UnifiedLogSource()
    first = source.pull()
    assert len(first) == 2

    _insert_row(room_id="master_room", message="new_c")
    second = source.pull()

    assert [e.content for e in second] == ["new_c"]


def test_cursor_persists_across_source_instances():
    _wind_cursor_to_current_max()
    _insert_row(room_id="master_room", message="first")

    source_a = UnifiedLogSource()
    envelopes_a = source_a.pull()
    assert len(envelopes_a) == 1

    _insert_row(room_id="master_room", message="second")

    source_b = UnifiedLogSource()
    envelopes_b = source_b.pull()
    assert [e.content for e in envelopes_b] == ["second"]


def test_empty_message_rows_skipped_but_cursor_advances():
    _wind_cursor_to_current_max()
    _insert_row(room_id="master_room", message="   ")
    _insert_row(room_id="master_room", message="real")

    source = UnifiedLogSource()
    envelopes = source.pull()

    assert [e.content for e in envelopes] == ["real"]
    # Cursor is past both rows → second pull is empty, not the "real" row again.
    assert source.pull() == []


def test_envelope_metadata_includes_room_and_speaker_fields():
    _wind_cursor_to_current_max()
    _insert_row(
        room_id="master_room",
        message="hi",
        speaker_name="Jukka",
        direction="inbound",
    )

    envelopes = UnifiedLogSource().pull()

    assert len(envelopes) == 1
    m = envelopes[0].metadata
    assert m["room_id"] == "master_room"
    assert m["speaker_name"] == "Jukka"
    assert m["direction"] == "inbound"
    assert "unified_log_metadata" in m


def test_custom_room_inclusion_list():
    """Caller can override defaults to include only specific rooms."""
    _wind_cursor_to_current_max()
    _insert_row(room_id="master_room", message="default_would_match")
    _insert_row(room_id="custom_room", message="custom_match")

    source = UnifiedLogSource(
        included_rooms=["custom_room"],
        included_room_prefixes=[],
    )
    envelopes = source.pull()

    assert len(envelopes) == 1
    assert envelopes[0].content == "custom_match"


def test_pull_on_nothing_new_returns_empty():
    _wind_cursor_to_current_max()
    source = UnifiedLogSource()
    assert source.pull() == []
