"""
Contract tests for GmailAPIClient.search_emails_metadata.

Regression anchors:
- BatchHttpRequest.execute() does NOT accept num_retries. If someone passes it,
  the runtime raises TypeError and the whole tool fails. This test watches for
  that by recording the kwargs passed to batch.execute().
- batch fetch retries on transient 429/5xx without passing unsupported kwargs.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import pytest
from googleapiclient.errors import HttpError


class _FakeResp:
    def __init__(self, status: int):
        self.status = status
        self.reason = "simulated"


class _FakeListRequest:
    def __init__(self, response):
        self._response = response

    def execute(self, **kwargs):
        return self._response


class _FakeMessageGet:
    def __init__(self, msg_id: str):
        self.id = msg_id


class _FakeBatch:
    """
    Mimics googleapiclient.http.BatchHttpRequest's minimal surface used by the
    client: `add(request, request_id=...)` and `execute()`. We deliberately
    make execute() reject unknown kwargs the same way the real class does —
    that's the bug we want to lock down.

    The side-effects list is shared across batches (owned by the service) so
    retry attempts consume from the same sequence — mirroring "each network
    call gets the next scheduled outcome."
    """
    def __init__(self, callback, shared_side_effects):
        self._callback = callback
        self._items = []  # list of (request_id, request)
        self._side_effects = shared_side_effects  # list, shared across batches
        self.execute_calls = []  # records kwargs to detect num_retries

    def add(self, request, request_id=None):
        self._items.append((request_id, request))

    def execute(self, **kwargs):
        # Real BatchHttpRequest.execute signature is (self, http=None) only.
        # Any other kwargs would TypeError. We mirror that here.
        unexpected = [k for k in kwargs if k != "http"]
        if unexpected:
            raise TypeError(
                f"execute() got an unexpected keyword argument {unexpected[0]!r}"
            )
        self.execute_calls.append(dict(kwargs))

        effect = self._side_effects.pop(0) if self._side_effects else None
        if isinstance(effect, Exception):
            raise effect

        # Success path: fire the callback once per enqueued sub-request with a
        # stub metadata response.
        for request_id, _req in self._items:
            self._callback(
                request_id,
                {
                    "id": request_id,
                    "internalDate": "1700000000000",
                    "payload": {"headers": [
                        {"name": "From", "value": "someone@example.com"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "Tue, 01 Jan 2026 12:00:00 +0000"},
                        {"name": "To", "value": "me@example.com"},
                    ]},
                },
                None,
            )
        self._items = []


class _FakeMessagesResource:
    def __init__(self, message_ids):
        self._message_ids = message_ids

    def list(self, **_):
        return _FakeListRequest({
            "messages": [{"id": mid, "threadId": f"t_{mid}"} for mid in self._message_ids],
            "nextPageToken": None,
        })

    def get(self, *, userId, id, format, metadataHeaders):
        return _FakeMessageGet(id)


class _FakeUsersResource:
    def __init__(self, message_ids):
        self._message_ids = message_ids

    def messages(self):
        return _FakeMessagesResource(self._message_ids)


class _FakeGmailService:
    def __init__(self, message_ids, batch_effects=None):
        self._message_ids = message_ids
        # Shared mutable list; every batch we hand out consumes from this
        # sequence, so the Nth execute() across any batch gets batch_effects[N].
        self._shared_effects = list(batch_effects or [])
        self.created_batches = []

    def users(self):
        return _FakeUsersResource(self._message_ids)

    def new_batch_http_request(self, callback):
        batch = _FakeBatch(callback, shared_side_effects=self._shared_effects)
        self.created_batches.append(batch)
        return batch


def _build_client(message_ids, batch_effects=None):
    from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient
    client = GmailAPIClient.__new__(GmailAPIClient)
    client.service = _FakeGmailService(message_ids, batch_effects=batch_effects)
    return client


def test_search_emails_metadata_does_not_pass_num_retries_to_batch_execute():
    """
    Regression: commit ea041951 passed num_retries=5 to BatchHttpRequest.execute
    (which doesn't accept it), causing every get_email_stats call to TypeError.
    """
    client = _build_client(message_ids=["m1", "m2", "m3"])

    result = client.search_emails_metadata("from:x@example.com", max_results=100, metadata_sample_size=10)

    assert result["total"] == 3
    # Verify the batch was executed at least once with no surprise kwargs.
    svc = client.service
    assert svc.created_batches, "search_emails_metadata did not create a batch"
    for batch in svc.created_batches:
        for call_kwargs in batch.execute_calls:
            assert "num_retries" not in call_kwargs, (
                "Regression: batch.execute() was called with num_retries — "
                "BatchHttpRequest.execute() doesn't accept that kwarg."
            )


def test_search_emails_metadata_retries_batch_on_transient_http_error():
    """
    After the manual retry loop was added, a 429 on the first attempt should
    be retried transparently; the caller sees a normal success.
    """
    transient_error = HttpError(resp=_FakeResp(429), content=b"Too many concurrent requests")
    # First execute() raises 429, second succeeds.
    client = _build_client(
        message_ids=["m1", "m2"],
        batch_effects=[transient_error, None],
    )

    result = client.search_emails_metadata("from:x@example.com", max_results=10, metadata_sample_size=5)

    assert result["total"] == 2
    # On the failed attempt, _items was not consumed and a fresh batch is created
    # for the retry. So we expect 2 batches total.
    assert len(client.service.created_batches) == 2


def test_search_emails_metadata_gives_up_and_raises_after_max_attempts():
    """
    Five 429s in a row should eventually propagate, not loop forever.
    """
    transient_error = HttpError(resp=_FakeResp(429), content=b"Too many concurrent requests")
    client = _build_client(
        message_ids=["m1"],
        batch_effects=[transient_error] * 10,
    )
    with pytest.raises(Exception):
        client.search_emails_metadata("from:x@example.com", max_results=10, metadata_sample_size=5)
