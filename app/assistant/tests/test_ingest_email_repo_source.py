"""Tests for ingest.sources.email_repo_source.EmailRepoSource.

Uses the test_setup harness. Tests operate against the real
EventRepository database — each test clears email rows and the ingest
cursor before running so it's independent of earlier state.

The cursor is manually wound back an hour on each test so freshly-inserted
rows reliably fall within the poll window regardless of clock resolution.
"""
import app.assistant.tests.test_setup  # noqa: F401

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.database.db_handler import EventRepository
from app.assistant.event_repository.event_repository import EventRepositoryManager
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.ingest.sources.email_repo_source import (
    EmailRepoSource,
    _CURSOR_KEY,
)
from app.models.base import get_session


def _clear_email_events() -> None:
    session = get_session()
    try:
        session.query(EventRepository).filter_by(data_type="email").delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _insert_email(
    repo: EventRepositoryManager,
    *,
    record_id: str,
    uid: str = "",
    account_id: str = "acct_default",
    sender: str = "Jane Smith",
    email_address: str = "jane@example.com",
    subject: str = "Hello",
    body: str = "body text",
    summary: str = "",
) -> None:
    repo.store_event(
        id=record_id,
        event_id=record_id,
        event_data={
            "uid": uid or record_id,
            "account_id": account_id,
            "sender": sender,
            "email_address": email_address,
            "subject": subject,
            "body": body,
            "summary": summary,
            "date_received": datetime.now(timezone.utc).isoformat(),
        },
        data_type="email",
    )


def _wind_cursor_back(seconds: int = 3600) -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    IngestCursorStore().set(source_key=_CURSOR_KEY, cursor_value=past.isoformat())


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_email_events()
    IngestCursorStore().clear(_CURSOR_KEY)
    yield
    _clear_email_events()
    IngestCursorStore().clear(_CURSOR_KEY)


def test_pull_returns_envelopes_for_new_email_events():
    repo = EventRepositoryManager()
    _wind_cursor_back()

    _insert_email(repo, record_id="email-1", sender="Alice", email_address="alice@example.com", subject="Re: meeting")
    _insert_email(repo, record_id="email-2", sender="Bob", email_address="bob@example.com", subject="Invoice")

    source = EmailRepoSource()
    envelopes = source.pull()

    assert len(envelopes) == 2
    subjects = {e.metadata.get("subject") for e in envelopes}
    assert subjects == {"Re: meeting", "Invoice"}
    assert all(e.source_type == "external" for e in envelopes)
    assert all(e.source_id == "event_repository:email" for e in envelopes)
    assert all(e.signal_type == "email" for e in envelopes)


def test_envelope_sender_fields_are_normalized():
    repo = EventRepositoryManager()
    _wind_cursor_back()
    _insert_email(
        repo,
        record_id="email-sender",
        sender="Alice",
        email_address="Alice@Example.COM",
    )

    envelopes = EmailRepoSource().pull()

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.metadata["sender_display"] == "Alice"
    assert env.metadata["sender_email"] == "alice@example.com"
    assert env.data["sender_email"] == "alice@example.com"


def test_second_pull_returns_empty_when_no_new_rows():
    repo = EventRepositoryManager()
    _wind_cursor_back()
    _insert_email(repo, record_id="email-a")

    source = EmailRepoSource()
    first = source.pull()
    second = source.pull()

    assert len(first) == 1
    assert second == []


def test_cursor_persists_across_source_instances():
    repo = EventRepositoryManager()
    _wind_cursor_back()
    _insert_email(repo, record_id="email-old")

    source_a = EmailRepoSource()
    envelopes_a = source_a.pull()
    assert len(envelopes_a) == 1

    # Cursor advance uses sub-second timestamps; the repo filter is strict
    # (created_at > since_dt). Sleep briefly so the next insert's created_at
    # lands strictly after the cursor. Production cadence is always multiple
    # seconds so this doesn't hide a real bug.
    time.sleep(1.1)
    _insert_email(repo, record_id="email-new")

    source_b = EmailRepoSource()
    envelopes_b = source_b.pull()

    assert len(envelopes_b) == 1
    assert envelopes_b[0].metadata.get("uid") == "email-new"


def test_first_run_without_cursor_starts_from_now():
    """Fresh state (no cursor) should start from now — never re-ingest history."""
    repo = EventRepositoryManager()
    # Insert an email BEFORE constructing the source — should be ignored.
    _insert_email(repo, record_id="email-pre-existing")

    source = EmailRepoSource()
    envelopes = source.pull()

    assert envelopes == []


def test_missing_uid_gets_content_hash_signal_id():
    repo = EventRepositoryManager()
    _wind_cursor_back()
    _insert_email(repo, record_id="email-no-uid", uid="")

    envelopes = EmailRepoSource().pull()

    # store_event defaults uid to record_id via _insert_email's uid="" -> uid=record_id.
    # Check envelope signal_id is the account::uid form.
    assert len(envelopes) == 1
    assert envelopes[0].signal_id.startswith("repo_email::acct_default::")


def test_pull_on_empty_repo_returns_empty_list():
    _wind_cursor_back()
    source = EmailRepoSource()
    assert source.pull() == []
