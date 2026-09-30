"""Emails live only in the pod store (pod_store/email_pods.py), 2026-09-29.

Pins: the pod id is the one every existing email pod already has; a parsed email becomes one pod
holding the full body and every header and parser field; storing it twice changes nothing; readers
select by when the email ARRIVED, so a pod minted late is not a recent email; the ingest source
forwards only freshly fetched email to the signal router, in the envelope shape it always read.
Fake pod store; invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.pod_store import email_pods
from app.assistant.pod_store.email_pods import (
    FETCH_CREATOR, MIGRATION_CREATOR, build_email_pod, email_pod_id, email_record, put_email_pod, recent_emails,
)

NOW = datetime.now(timezone.utc)


def _email(uid="u1", importance=7, received=NOW, **kw):
    return {"uid": uid, "account_id": "acct", "subject": "Field trip form", "sender": "School Office",
            "email_address": "Office@School.example", "summary": "Form due Friday.",
            "action_items": ["sign the form"], "importance": importance, "thread_id": "t1",
            "date_received": received.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "body": "Please sign and return the attached form by Friday.", **kw}


class _Store:
    def __init__(self):
        self.pods = {}

    def get(self, pod_id):
        return self.pods.get(pod_id)

    def put(self, pod):
        pod.created_at = pod.created_at or datetime.now(timezone.utc)
        self.pods[pod.pod_id] = pod

    def query(self, *, kind=None, since_utc=None, limit=None):
        return [p for p in sorted(self.pods.values(), key=lambda p: p.created_at, reverse=True)
                if p.kind == kind and (since_utc is None or p.created_at >= since_utc)]


def test_the_pod_id_is_the_one_existing_email_pods_have():
    # sha256("repo_email::acct::u1")[:24], the derivation the ingest copy step used.
    assert email_pod_id("acct", "u1") == build_email_pod(_email()).pod_id
    assert email_pod_id("", "u1") == email_pod_id("unknown_account", "u1")


def test_a_parsed_email_becomes_one_complete_pod():
    pod = build_email_pod(_email())
    assert pod.kind == "email" and pod.one_liner == "School Office: Field trip form"
    assert pod.body == "Please sign and return the attached form by Friday."
    for key in ("summary", "action_items", "importance", "thread_id", "date_received", "subject"):
        assert key in pod.metadata, key
    assert "body" not in pod.metadata
    assert pod.metadata["sender_email"] == "office@school.example"
    assert pod.source_refs[0].kind == "gmail" and pod.source_refs[0].id == "acct:u1"
    assert pod.importance == 7.0 and pod.created_by == FETCH_CREATOR
    record = email_record(pod)
    assert record["body"] == pod.body and record["pod_id"] == pod.pod_id and record["summary"] == "Form due Friday."


def test_storing_the_same_email_twice_changes_nothing():
    store = _Store()
    first = put_email_pod(_email(), store=store)
    again = put_email_pod(_email(summary="rewritten"), store=store)
    assert first == (email_pod_id("acct", "u1"), True) and again == (first[0], False)
    assert store.get(first[0]).metadata["summary"] == "Form due Friday."


def test_recent_means_received_recently_and_important():
    store = _Store()
    put_email_pod(_email("new", received=NOW - timedelta(hours=1)), store=store)
    put_email_pod(_email("old", received=NOW - timedelta(days=30)), store=store)     # minted late
    put_email_pod(_email("minor", importance=3, received=NOW - timedelta(hours=1)), store=store)
    got = recent_emails(received_since=NOW - timedelta(hours=10), store=store)
    assert [r["uid"] for r in got] == ["new"]


def test_the_ingest_source_forwards_only_freshly_fetched_email(monkeypatch):
    from app.assistant.ingest.sources.email_pod_source import EmailPodSource

    class _Cursor:
        def __init__(self):
            self.value = (NOW - timedelta(hours=1)).isoformat()

        def get(self, key):
            return self.value

        def set(self, *, source_key, cursor_value):
            self.value = cursor_value

    store = _Store()
    fresh_id, _ = put_email_pod(_email("fresh"), store=store)
    store.pods[fresh_id].created_at = NOW - timedelta(minutes=30)     # explicit: no clock-tick races
    backlog = build_email_pod(_email("backlog"))
    backlog.created_by = MIGRATION_CREATOR
    backlog.created_at = NOW - timedelta(minutes=30)
    store.put(backlog)
    source = EmailPodSource(cursor_store=_Cursor(), store=store)
    [env] = source.pull()
    assert env.signal_type == "email" and env.signal_id == "repo_email::acct::fresh"
    assert env.data["subject"] == "Field trip form" and env.data["body"].startswith("Please sign")
    assert env.data["sender_email"] == "office@school.example"
    assert source.pull() == [], "the cursor advanced"
