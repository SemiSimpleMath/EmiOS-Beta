"""Integration test for Step A2: the gut → signal_router wiring.

Creates a real IngestService with a real UnifiedLogSource, registers
SignalRouterService.handle_envelope as a subscriber, inserts a row into
unified_log_2026, ticks the gut once, and verifies signal_router saw the
envelope (by checking that its dedupe table recorded a (watch, signal_id)
key for a matching watch).

This is the shape the bootstrap uses in production.
"""
import app.assistant.tests.test_setup  # noqa: F401

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.ingest.ingest_service import IngestService
from app.assistant.ingest.sources.unified_log_source import (
    UnifiedLogSource,
    _CURSOR_KEY as UNIFIED_LOG_CURSOR_KEY,
)
from app.assistant.signal_router.contracts import WatcherAgentOutput
from app.assistant.signal_router.signal_router_service import SignalRouterService
from app.models.base import get_session


_TEST_SOURCE = "wiring_test_fixture"


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


def _insert_master_room_row(message: str) -> str:
    row_id = f"wiring-{uuid.uuid4()}"
    session = get_session()
    try:
        session.add(
            UnifiedLog2026(
                id=row_id,
                timestamp=datetime.now(timezone.utc),
                role="user",
                message=message,
                source=_TEST_SOURCE,
                processed=False,
                room_id="master_room",
                direction="inbound",
                speaker_name="test_user",
            )
        )
        session.commit()
    finally:
        session.close()
    return row_id


def _current_max_rowid() -> int:
    from sqlalchemy import text
    session = get_session()
    try:
        result = session.execute(text("SELECT MAX(rowid) FROM unified_log_2026")).scalar()
        return int(result) if result is not None else 0
    finally:
        session.close()


class _StubWatcher:
    def __init__(self):
        self.calls = 0

    def evaluate(self, *, watch_registration, signal, filter_decision):
        self.calls += 1
        return WatcherAgentOutput(
            should_emit_event=True,
            match_reason="wiring_test_match",
            confidence=0.99,
            dedupe_key_hint=f"stub::{signal.get('signal_id','')}",
            evidence={},
            proposed_payload={},
        )


_TEST_WATCH_KEY_PREFIXES = ("wiring_watch_",)


def _cancel_test_watches() -> None:
    """Cancel watches this test file creates so they don't pollute the
    live signal_router_watch table. Keyed by prefix so a stray pattern
    match on real user watches can't happen."""
    from sqlalchemy import text
    session = get_session()
    try:
        for prefix in _TEST_WATCH_KEY_PREFIXES:
            session.execute(
                text(
                    "UPDATE signal_router_watch SET status='cancelled' "
                    "WHERE status='active' AND watch_key LIKE :pfx"
                ),
                {"pfx": f"{prefix}%"},
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_test_rows()
    IngestCursorStore().clear(UNIFIED_LOG_CURSOR_KEY)
    _cancel_test_watches()
    yield
    _clear_test_rows()
    IngestCursorStore().clear(UNIFIED_LOG_CURSOR_KEY)
    _cancel_test_watches()


def test_gut_dispatches_envelope_to_signal_router_handle_envelope():
    # Wind ingest cursor to "now" so we only see rows inserted by this test.
    IngestCursorStore().set(
        source_key=UNIFIED_LOG_CURSOR_KEY,
        cursor_value=str(_current_max_rowid()),
    )

    signal_router = SignalRouterService(emit_to_event_hub=False)
    signal_router._watcher_service = _StubWatcher()

    reg = signal_router.register_keyword_watch(
        watch_key="wiring_watch_mango",
        event_name="signal_router.watch.wiring_mango",
        keywords=["mango"],
    )

    gut = IngestService(
        sources=[UnifiedLogSource()],
        poll_interval_seconds=60,
    )
    gut.register_subscriber(signal_router.handle_envelope)

    row_id = _insert_master_room_row("I need a mango smoothie")

    dispatched = gut.tick_once()

    assert dispatched == 1, "gut should have pulled and dispatched exactly one envelope"

    # Verify signal_router processed it end-to-end: dedupe row written for this
    # (watch, signal_id) pair.
    assert signal_router._state_store.has_dedupe_key(f"{reg.registration_id}:{row_id}")


def test_gut_wiring_does_not_crash_with_no_subscribers_or_no_watches():
    IngestCursorStore().set(
        source_key=UNIFIED_LOG_CURSOR_KEY,
        cursor_value=str(_current_max_rowid()),
    )

    # Gut exists; no subscribers registered.
    gut = IngestService(sources=[UnifiedLogSource()], poll_interval_seconds=60)

    _insert_master_room_row("isolated message no one cares about")

    # tick_once still returns the count — subscribers is just empty.
    dispatched = gut.tick_once()
    assert dispatched == 1


def test_gut_wiring_with_signal_router_no_watches_is_safe():
    IngestCursorStore().set(
        source_key=UNIFIED_LOG_CURSOR_KEY,
        cursor_value=str(_current_max_rowid()),
    )

    signal_router = SignalRouterService(emit_to_event_hub=False)
    signal_router._watcher_service = _StubWatcher()
    # No watches registered — handle_envelope will iterate over empty list.

    gut = IngestService(sources=[UnifiedLogSource()], poll_interval_seconds=60)
    gut.register_subscriber(signal_router.handle_envelope)

    _insert_master_room_row("nothing to match")

    # Should dispatch successfully even though no match occurs.
    dispatched = gut.tick_once()
    assert dispatched == 1
    assert signal_router._watcher_service.calls == 0, "watcher must not be invoked with zero watches"
