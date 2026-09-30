"""Email pod source for the gut (IngestService), 2026-09-30.

Emails live only in the pod store (pod_store/email_pods.py). This source emits one IngestEnvelope per
email pod the fetch stored since the last pull, for the gut's subscribers — the signal router's
email_semantic_match watches (a compiled task waiting for an email). Email pods are minted by the
fetch itself, so the pod classifier ignores these envelopes.

Pods made by the one-time move from the event repository are not forwarded: they are a backlog, not
new mail. The first pull starts at the current time and does not replay history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.pod_store.email_pods import FETCH_CREATOR, email_key, email_record
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_CURSOR_KEY = "pod_email_last_created_at_utc"


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EmailPodSource:
    """Pulls email pods stored since the last pull and emits IngestEnvelopes."""

    name = "email_pod"

    def __init__(self, cursor_store: Optional[IngestCursorStore] = None, store=None) -> None:
        from app.assistant.pod_store.pod_store import PodStore
        self._cursor_store = cursor_store or IngestCursorStore()
        self._store = store or PodStore()
        raw = self._cursor_store.get(_CURSOR_KEY)
        self._last_check_utc = (datetime.fromisoformat(raw) if raw else datetime.now(timezone.utc))
        if self._last_check_utc.tzinfo is None:
            self._last_check_utc = self._last_check_utc.replace(tzinfo=timezone.utc)

    def pull(self) -> List[IngestEnvelope]:
        # Half-open windows [previous cutoff, this cutoff): a pod created in the very instant a
        # window closes belongs to the next pull only, so no email is forwarded twice or skipped.
        query_end = datetime.now(timezone.utc)
        pods = self._store.query(kind="email", since_utc=self._last_check_utc, limit=None)
        envelopes = [self._envelope(p) for p in pods
                     if p.created_by == FETCH_CREATOR and _utc(p.created_at) < query_end]
        self._last_check_utc = query_end
        self._cursor_store.set(source_key=_CURSOR_KEY, cursor_value=query_end.isoformat())
        return envelopes

    @staticmethod
    def _envelope(pod) -> IngestEnvelope:
        record = email_record(pod)
        sender_display = str(record.get("sender_display") or record.get("sender") or "").strip()
        sender_email = str(record.get("sender_email") or record.get("email_address") or "").strip().lower()
        record["sender_display"] = sender_display
        record["sender_email"] = sender_email
        record.setdefault("email_address", sender_email)
        subject = str(record.get("subject") or "").strip()
        content = " | ".join(part for part in [sender_display or sender_email, subject,
                                              str(record.get("summary") or "").strip(),
                                              str(record.get("body") or "").strip()] if part)
        envelope = IngestEnvelope(
            signal_id=email_key(str(record.get("account_id") or ""), str(record.get("uid") or "")),
            source_type="external",
            source_id="pod_store:email",
            occurred_at_utc=str(record.get("received_at_utc") or record.get("date_received")
                                or datetime.now(timezone.utc).isoformat()),
            signal_type="email",
            content=content,
            data=record,
            metadata={"uid": record.get("uid"), "account_id": record.get("account_id"),
                      "sender_display": sender_display, "sender_email": sender_email,
                      "subject": subject, "pod_id": pod.pod_id},
        )
        envelope.validate()
        return envelope
