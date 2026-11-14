"""The calendar sync judges only the rows its fetch covered (2026-11-14).

The 7-day calendar sync deleted every stored calendar row missing from its fetch, including an event the
assistant had just created three weeks ahead; the brain read each prune as the owner removing the event.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.database.db_handler import EventRepository
from app.assistant.event_repository.event_repository import EventRepositoryManager


@pytest.fixture
def repo():
    engine = create_engine("sqlite:///:memory:")
    EventRepository.__table__.create(engine)
    manager = EventRepositoryManager(session_factory=sessionmaker(bind=engine))
    with patch("app.assistant.event_repository.event_repository.DI", MagicMock()):
        yield manager


def _store(repo, event_id, start):
    repo.store_event(event_id, event_data={"id": event_id, "summary": event_id, "start": start}, data_type="calendar")


def _ids(repo):
    import json
    return {e["data"]["id"] for e in json.loads(repo.search_events(data_type="calendar"))}


WINDOW_END = datetime(2026, 11, 21, 7, 59, 59, tzinfo=timezone.utc)


def test_row_starting_after_the_window_is_kept(repo):
    _store(repo, "in_week", "2026-11-16T18:00:00+00:00")
    _store(repo, "dec_7", "2026-12-07T18:45:00+00:00")
    repo.sync_events_with_server(["in_week"], "calendar", window_end=WINDOW_END)
    assert _ids(repo) == {"in_week", "dec_7"}


def test_row_inside_or_before_the_window_missing_from_the_fetch_is_deleted(repo):
    _store(repo, "kept", "2026-11-16T18:00:00+00:00")
    _store(repo, "deleted_in_week", "2026-11-17T18:00:00+00:00")
    _store(repo, "past", "2026-11-10T18:00:00+00:00")
    repo.sync_events_with_server(["kept"], "calendar", window_end=WINDOW_END)
    assert _ids(repo) == {"kept"}


def test_without_a_window_every_missing_row_is_deleted(repo):
    _store(repo, "kept", "2026-11-16T18:00:00+00:00")
    _store(repo, "dec_7", "2026-12-07T18:45:00+00:00")
    repo.sync_events_with_server(["kept"], "calendar")
    assert _ids(repo) == {"kept"}
