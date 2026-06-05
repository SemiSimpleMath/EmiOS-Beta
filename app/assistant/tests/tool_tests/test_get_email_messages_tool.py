"""
Tests for GetEmailMessagesTool.execute — the HEADERS-ONLY contract.

This tool is the *search* half of two-phase retrieval: it returns per-message
headers (subject/sender/date/message_id/snippet) plus the one high-signal
body-derived field (a meeting link), but NEVER full bodies. Full bodies are
hydrated on demand via get_email_thread(message_id). Returning full bodies
inline was a context bomb (2026-06 Friday-Night-Meats incident). These tests
lock in: headers + snippet + meeting_link are surfaced in ``content``; the
message_id (hydrate handle) is present; and no full body is returned.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.tests.tool_tests._helpers import make_tool_message


# ----------------------------------------------------------------------
# Minimal Gmail client fake
# ----------------------------------------------------------------------

@dataclass
class _FakeEmail:
    uid: str
    subject: str
    sender: str
    sender_email: str
    date: str  # RFC 2822, e.g. "Fri, 17 Apr 2026 20:30:00 +0000"
    body: str
    thread_id: str = ""

    def as_raw(self) -> str:
        """Assemble a RFC 822 email string matching what fetch_full_email returns."""
        return (
            f"Message-ID: <{self.uid}@test.local>\n"
            f"Subject: {self.subject}\n"
            f"From: {self.sender} <{self.sender_email}>\n"
            f"Date: {self.date}\n"
            "Content-Type: text/plain; charset=\"UTF-8\"\n"
            "\n"
            f"{self.body}"
        )


class _FakeGmailClient:
    def __init__(self, emails: list[_FakeEmail]):
        self._emails = emails

    def search_emails(self, query: str, max_results: int = 25):
        # Ignore query in tests; return all canned emails.
        out = []
        for e in self._emails[:max_results]:
            out.append({"uid": e.uid, "threadId": e.thread_id or f"thread-{e.uid}"})
        return out

    def fetch_full_email(self, message_id: str):
        for e in self._emails:
            if e.uid == message_id:
                return {"raw_email": e.as_raw()}
        return None


class _FakeEmailUtils:
    def __init__(self, client: _FakeGmailClient):
        self._client = client

    def get_gmail_client(self, _account_id):
        return self._client


def _build_tool(canned_emails: list[_FakeEmail]):
    """Build a GetEmailMessagesTool instance wired to a fake Gmail client."""
    from app.assistant.lib.tools.get_email_messages.get_email_messages import (
        GetEmailMessagesTool,
    )
    tool = GetEmailMessagesTool()
    tool._email_utils = _FakeEmailUtils(_FakeGmailClient(canned_emails))  # bypass real client
    return tool


def _call_tool(tool, **args):
    msg = make_tool_message("get_email_messages", args)
    return tool.execute(msg)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_content_includes_subject_and_sender():
    """Each matched email's subject and sender must appear in ``content``."""
    tool = _build_tool([
        _FakeEmail(
            uid="m1",
            subject="Friday Night Meats - Zoom link",
            sender="Bob Example",
            sender_email="bob@example.com",
            date="Fri, 17 Apr 2026 20:30:00 +0000",
            body="Here's the link: https://us02web.zoom.us/j/83742000488",
        ),
    ])
    result = _call_tool(tool, query="Friday Night Meats", account_id="primary")
    content = result.content or ""
    assert "Friday Night Meats - Zoom link" in content, (
        f"Subject missing from content. content was:\n{content}"
    )
    assert "bob@example.com" in content or "Bob Example" in content, (
        f"Sender missing from content. content was:\n{content}"
    )


def test_content_surfaces_zoom_link_from_body():
    """A Zoom URL anywhere in the body must be lifted into ``content``."""
    zoom_url = "https://us02web.zoom.us/j/83742000488?pwd=abc123"
    tool = _build_tool([
        _FakeEmail(
            uid="m1",
            subject="FNM invite",
            sender="Bob",
            sender_email="bob@example.com",
            date="Fri, 17 Apr 2026 20:30:00 +0000",
            body=(
                "Hey Jukka,\n\n"
                "Here's the link for tonight:\n"
                f"{zoom_url}\n"
                "Meeting ID: 837 4200 0488"
            ),
        ),
    ])
    result = _call_tool(tool, query="anything", account_id="primary")
    content = result.content or ""
    assert "us02web.zoom.us/j/83742000488" in content, (
        f"Zoom link missing from content. content was:\n{content}"
    )


def test_content_surfaces_google_meet_link_from_body():
    """Google Meet links should also be surfaced (parity with Zoom)."""
    meet_url = "https://meet.google.com/abc-defg-hij"
    tool = _build_tool([
        _FakeEmail(
            uid="m1",
            subject="Weekly 1:1",
            sender="Manager",
            sender_email="mgr@example.com",
            date="Fri, 17 Apr 2026 20:30:00 +0000",
            body=f"Same link as always: {meet_url}",
        ),
    ])
    result = _call_tool(tool, query="anything", account_id="primary")
    content = result.content or ""
    assert "meet.google.com/abc-defg-hij" in content, (
        f"Meet link missing from content. content was:\n{content}"
    )


def test_content_lists_each_message_on_its_own_indexed_line():
    """Multiple matches should each get an indexed line in the summary."""
    tool = _build_tool([
        _FakeEmail(uid="m1", subject="First",  sender="A", sender_email="a@ex.com",
                   date="Fri, 17 Apr 2026 20:30:00 +0000", body="hello"),
        _FakeEmail(uid="m2", subject="Second", sender="B", sender_email="b@ex.com",
                   date="Fri, 17 Apr 2026 21:30:00 +0000", body="world"),
        _FakeEmail(uid="m3", subject="Third",  sender="C", sender_email="c@ex.com",
                   date="Fri, 17 Apr 2026 22:30:00 +0000", body="more"),
    ])
    result = _call_tool(tool, query="whatever", account_id="primary")
    content = result.content or ""
    assert "[1]" in content and "[2]" in content and "[3]" in content, (
        f"Indexed lines missing from content. content was:\n{content}"
    )
    assert "First" in content and "Second" in content and "Third" in content


def test_content_header_reports_match_count():
    """Count should still be present for the agent to gauge breadth of results."""
    tool = _build_tool([
        _FakeEmail(uid=f"m{i}", subject=f"Subject {i}", sender=f"S{i}",
                   sender_email=f"s{i}@ex.com",
                   date="Fri, 17 Apr 2026 20:30:00 +0000", body=f"body {i}")
        for i in range(5)
    ])
    result = _call_tool(tool, query="any", account_id="primary")
    content = result.content or ""
    assert "Fetched 5 message" in content, (
        f"Match count header missing. content was:\n{content}"
    )


def test_data_dict_carries_header_rows_not_bodies():
    """``data.messages`` carries lightweight header rows — message_id (the hydrate
    handle) + snippet + meeting_link — but NEVER a full body."""
    tool = _build_tool([
        _FakeEmail(
            uid="m1",
            subject="FNM",
            sender="Bob",
            sender_email="bob@example.com",
            date="Fri, 17 Apr 2026 20:30:00 +0000",
            body="https://us02web.zoom.us/j/999",
        ),
    ])
    result = _call_tool(tool, query="anything", account_id="primary")
    assert isinstance(result.data, dict)
    msgs = result.data.get("messages")
    assert isinstance(msgs, list) and len(msgs) == 1
    row = msgs[0]
    assert row["subject"] == "FNM"
    assert row["sender"] == "Bob"
    assert row["message_id"] == "m1"            # hydrate handle present
    assert "body" not in row                     # full body must NOT be returned
    assert row["meeting_link"] == "https://us02web.zoom.us/j/999"
    assert "us02web.zoom.us/j/999" in row["snippet"]


def test_zero_hits_produces_clean_header():
    """An empty query result must produce a readable content, not crash."""
    tool = _build_tool([])
    result = _call_tool(tool, query="nothing matches", account_id="primary")
    content = result.content or ""
    assert "Fetched 0 message" in content
