"""
Contract tests for trash_emails.

trash_emails is destructive: it moves mail to Trash in bulk, gated only by
the approval UX and whatever query the planner built. This file locks down:
  - Argument validation (senders required, normalized from str to list).
  - Fan-out: each sender gets its own batch_trash_by_sender call.
  - Totals are summed across senders.
  - The per-sender cap is NOT controllable by the caller (product decision
    in trash_emails.py — any caller-provided max_per_sender is ignored).
  - extra_query is forwarded to Gmail verbatim.
  - Tool-level errors are returned as ToolResult errors, not raised.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.tests.tool_tests._helpers import make_tool_message


class _RecordingGmailClient:
    """Stand-in for GmailAPIClient that records each batch_trash_by_sender call."""

    def __init__(self, *, account_id=None):
        self.account_id = account_id
        self.calls = []  # list of dicts: {"sender_email", "extra_query", "max_results"}

    def batch_trash_by_sender(self, *, sender_email, extra_query="", max_results=1000):
        self.calls.append({
            "sender_email": sender_email,
            "extra_query": extra_query,
            "max_results": max_results,
        })
        # Pretend we trashed a sender-specific number so totals are verifiable.
        trashed = 10 + len(self.calls)  # 11 for first sender, 12 for second, etc.
        return {"trashed": trashed, "message_ids": [f"msg-{i}" for i in range(trashed)]}


def _patch_gmail(monkeypatch):
    """
    Replace the GmailAPIClient symbol inside trash_emails so the tool uses our
    recording stand-in instead of auth'ing against Gmail.
    Returns the last-created client so tests can assert on its calls.
    """
    from app.assistant.lib.tools.trash_emails import trash_emails as trash_mod

    holder: dict = {"client": None}

    def factory(*, account_id=None):
        client = _RecordingGmailClient(account_id=account_id)
        holder["client"] = client
        return client

    monkeypatch.setattr(trash_mod, "GmailAPIClient", factory)
    return holder


def _build_tool():
    from app.assistant.lib.tools.trash_emails.trash_emails import TrashEmailsTool
    return TrashEmailsTool()


# ----------------------------------------------------------------------
# Argument validation
# ----------------------------------------------------------------------

def test_missing_senders_returns_tool_error(monkeypatch):
    _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {}))
    assert result.result_type == "error"


def test_empty_list_senders_returns_tool_error(monkeypatch):
    _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {"senders": []}))
    assert result.result_type == "error"


def test_whitespace_only_senders_returns_tool_error(monkeypatch):
    """Entries that are empty after strip() must not survive normalization."""
    _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {"senders": ["   ", ""]}))
    assert result.result_type == "error"


# ----------------------------------------------------------------------
# Normalization + fan-out
# ----------------------------------------------------------------------

def test_single_string_sender_gets_normalized_to_list(monkeypatch):
    """Planners sometimes pass a single string; the tool must not iterate characters."""
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {"senders": "noreply@example.com"}))
    assert result.result_type == "trash_emails"
    calls = holder["client"].calls
    assert len(calls) == 1
    assert calls[0]["sender_email"] == "noreply@example.com"


def test_each_sender_gets_its_own_batch_call(monkeypatch):
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {
        "senders": ["alice@example.com", "bob@example.com", "c@example.com"],
    }))
    assert result.result_type == "trash_emails"
    senders_called = [c["sender_email"] for c in holder["client"].calls]
    assert senders_called == ["alice@example.com", "bob@example.com", "c@example.com"]


def test_total_trashed_sums_across_senders(monkeypatch):
    """Fake client returns 11, 12, 13 for the three calls; summary should reflect 36 total."""
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {
        "senders": ["a@example.com", "b@example.com", "c@example.com"],
    }))
    assert result.result_type == "trash_emails"
    assert result.data["total_trashed"] == 11 + 12 + 13
    assert len(result.data["results"]) == 3
    assert "Trashed 36 email(s) across 3 sender(s)." == result.content


# ----------------------------------------------------------------------
# Product invariants
# ----------------------------------------------------------------------

def test_caller_provided_max_per_sender_is_ignored(monkeypatch):
    """
    trash_emails.py intentionally hardcodes max_per_sender to avoid inconsistent
    caps across runs. A caller-supplied value must NOT reach batch_trash_by_sender.
    """
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    tool.execute(make_tool_message("trash_emails", {
        "senders": ["alice@example.com"],
        "max_per_sender": 42,  # should be ignored
    }))
    call = holder["client"].calls[0]
    assert call["max_results"] != 42, (
        "Regression: trash_emails forwarded caller-provided max_per_sender. "
        "The hardcoded cap in trash_emails.py is deliberate."
    )
    # Current hardcoded value is 1000; lock that in explicitly.
    assert call["max_results"] == 1000


def test_extra_query_is_forwarded_verbatim(monkeypatch):
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    tool.execute(make_tool_message("trash_emails", {
        "senders": ["alice@example.com"],
        "extra_query": "before:2024/01/01",
    }))
    assert holder["client"].calls[0]["extra_query"] == "before:2024/01/01"


def test_account_id_is_passed_to_gmail_client(monkeypatch):
    holder = _patch_gmail(monkeypatch)
    tool = _build_tool()
    tool.execute(make_tool_message("trash_emails", {
        "senders": ["alice@example.com"],
        "account_id": "owner@example.com",
    }))
    assert holder["client"].account_id == "owner@example.com"


# ----------------------------------------------------------------------
# Error surfacing
# ----------------------------------------------------------------------

def test_gmail_api_failure_is_returned_as_tool_error_not_raised(monkeypatch):
    """If batch_trash_by_sender blows up, the tool must surface an error result, not propagate."""
    from app.assistant.lib.tools.trash_emails import trash_emails as trash_mod

    class _BoomClient:
        def __init__(self, *, account_id=None):
            pass
        def batch_trash_by_sender(self, **_):
            raise RuntimeError("simulated Gmail outage")

    monkeypatch.setattr(trash_mod, "GmailAPIClient", _BoomClient)
    tool = _build_tool()
    result = tool.execute(make_tool_message("trash_emails", {"senders": ["a@example.com"]}))
    assert result.result_type == "error"
