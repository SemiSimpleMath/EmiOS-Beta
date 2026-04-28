"""Email repo source for the gut.

Lifted from ``SignalRouterService._scan_repo_email_once`` +
``_repo_email_to_signal``. Behavior matches today's signal_router email
scan: pulls every email row inserted into EventRepository since the last
check timestamp, normalizes each into an IngestEnvelope, and advances
the cursor to the instant the poll started.

First-ever call starts from the current time (does not re-ingest
historical emails), mirroring signal_router behavior.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.event_repository.event_repository import EventRepositoryManager
from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


_CURSOR_KEY = "repo_email_last_created_at_utc"


class EmailRepoSource:
    """Pulls new email events from EventRepository and emits IngestEnvelopes."""

    name = "email_repo"

    def __init__(
        self,
        cursor_store: Optional[IngestCursorStore] = None,
        repo: Optional[EventRepositoryManager] = None,
    ) -> None:
        self._cursor_store = cursor_store or IngestCursorStore()
        self._repo = repo or EventRepositoryManager()
        self._last_check_utc: datetime = self._load_cursor_or_now()

    def _load_cursor_or_now(self) -> datetime:
        raw = self._cursor_store.get(_CURSOR_KEY)
        if not raw:
            return datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def pull(self) -> List[IngestEnvelope]:
        query_start = self._last_check_utc
        query_end = datetime.now(timezone.utc)
        items = self._repo.get_new_events_typed("email", query_start)

        envelopes: List[IngestEnvelope] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            envelope = self._email_to_envelope(data)
            envelopes.append(envelope)

        self._last_check_utc = query_end
        self._cursor_store.set(
            source_key=_CURSOR_KEY,
            cursor_value=query_end.isoformat(),
        )
        return envelopes

    @staticmethod
    def _email_to_envelope(email_data: Dict[str, Any]) -> IngestEnvelope:
        uid = str(email_data.get("uid") or "").strip()
        account_id = str(email_data.get("account_id") or "").strip()
        subject = str(email_data.get("subject") or "").strip()
        summary = str(email_data.get("summary") or "").strip()
        body = str(email_data.get("body") or "").strip()
        occurred = (
            str(email_data.get("date_received") or "").strip()
            or datetime.now(timezone.utc).isoformat()
        )

        # Canonical sender fields, normalized once here so downstream consumers
        # (garbage filter, watcher agent, pod classifier) use the same names.
        sender_display = str(email_data.get("sender") or "").strip()
        sender_email = str(email_data.get("email_address") or "").strip().lower()
        if not sender_email and "@" in sender_display:
            sender_email = sender_display.lower()

        signal_id = (
            f"repo_email::{account_id or 'unknown_account'}::{uid}"
            if uid
            else "repo_email::{}".format(
                hashlib.sha256(json.dumps(email_data, sort_keys=True).encode()).hexdigest()[:24]
            )
        )
        content = " | ".join(
            part for part in [sender_display or sender_email, subject, summary, body] if part
        )

        normalized_data = dict(email_data)
        normalized_data["sender_display"] = sender_display
        normalized_data["sender_email"] = sender_email
        if not normalized_data.get("email_address"):
            normalized_data["email_address"] = sender_email

        envelope = IngestEnvelope(
            signal_id=signal_id,
            source_type="external",
            source_id="event_repository:email",
            occurred_at_utc=occurred,
            signal_type="email",
            content=content,
            data=normalized_data,
            metadata={
                "uid": uid,
                "account_id": account_id,
                "sender_display": sender_display,
                "sender_email": sender_email,
                "subject": subject,
            },
        )
        envelope.validate()
        return envelope
