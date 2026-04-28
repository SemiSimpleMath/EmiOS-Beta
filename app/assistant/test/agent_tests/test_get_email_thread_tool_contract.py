import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.tools.get_email_thread.get_email_thread import GetEmailThreadTool
from app.assistant.utils.pydantic_classes import ToolMessage


class _StubGmailClient:
    def __init__(self):
        self._query_results = {}

    def set_query_results(self, query, rows):
        self._query_results[query] = rows

    def search_emails(self, query, max_results=20):
        _ = max_results
        return list(self._query_results.get(query, []))

    def get_thread_id_for_message(self, seed_message_id):
        _ = seed_message_id
        return ""

    def fetch_full_email(self, message_id):
        return {
            "uid": message_id,
            "raw_email": (
                "From: Mark <mark@example.com>\n"
                "To: Emi <emi@example.com>\n"
                "Subject: Cabin update\n"
                "Date: Mon, 01 Jan 2026 10:00:00 +0000\n"
                "\n"
                "Quick update from Mark."
            ),
        }


class _StubEmailUtils:
    def __init__(self):
        self.gmail = _StubGmailClient()

    def get_gmail_client(self, account_id=None):
        _ = account_id
        return self.gmail

    def fetch_full_thread(self, **kwargs):
        _ = kwargs
        return []


def _msg(arguments: dict) -> ToolMessage:
    return ToolMessage(
        tool_name="get_email_thread",
        tool_data={"tool_name": "get_email_thread", "arguments": arguments},
    )


def test_get_email_thread_no_match_returns_empty_non_error_result():
    tool = GetEmailThreadTool()
    tool._email_utils = _StubEmailUtils()
    result = tool.execute(
        _msg(
            {
                "participant_email": "no_thread@example.com",
                "account_id": "google_emi",
                "max_messages": 50,
                "include_body": True,
            }
        )
    )

    assert result.result_type == "get_email_thread"
    assert isinstance(result.data, dict)
    assert result.data.get("thread_found") is False
    assert result.data.get("message_count") == 0
    assert result.data.get("messages") == []


def test_get_email_thread_missing_identifiers_returns_structured_error():
    tool = GetEmailThreadTool()
    tool._email_utils = _StubEmailUtils()
    result = tool.execute(_msg({"account_id": "google_emi"}))

    assert result.result_type == "error"
    assert isinstance(result.data, dict)
    assert result.data.get("error_code") == "get_email_thread_missing_identifier"
    assert result.data.get("abort_policy") == "abort_tool"


def test_get_email_thread_prefers_inbound_participant_thread_and_returns_recent_context():
    tool = GetEmailThreadTool()
    tool._email_utils = _StubEmailUtils()
    gmail = tool._email_utils.gmail
    gmail.set_query_results(
        "from:mark@example.com",
        [{"uid": "m1", "threadId": "thread_inbound"}],
    )
    gmail.set_query_results(
        "(from:mark@example.com OR to:mark@example.com)",
        [{"uid": "m2", "threadId": "thread_other"}],
    )

    result = tool.execute(
        _msg(
            {
                "participant_email": "mark@example.com",
                "account_id": "google_emi",
                "include_recent_messages": True,
                "recent_message_limit": 3,
            }
        )
    )

    assert result.result_type == "get_email_thread"
    assert isinstance(result.data, dict)
    assert result.data.get("thread_id") == "thread_inbound"
    assert result.data.get("thread_found") is True
    assert isinstance(result.data.get("recent_messages"), list)
    assert result.data.get("recent_message_count") == 2
    assert result.data.get("recent_thread_ids") == ["thread_inbound", "thread_other"]


def test_get_email_thread_with_lookback_hours_applies_after_filter_to_queries():
    tool = GetEmailThreadTool()
    tool._email_utils = _StubEmailUtils()
    gmail = tool._email_utils.gmail
    gmail.set_query_results(
        "from:mark@example.com after:0",
        [{"uid": "m1", "threadId": "thread_inbound"}],
    )
    gmail.set_query_results(
        "(from:mark@example.com OR to:mark@example.com) after:0",
        [{"uid": "m2", "threadId": "thread_other"}],
    )

    original_search = gmail.search_emails

    def _patched_search(query, max_results=20):
        _ = max_results
        if query.startswith("from:mark@example.com after:"):
            return [{"uid": "m1", "threadId": "thread_inbound"}]
        if query.startswith("(from:mark@example.com OR to:mark@example.com) after:"):
            return [{"uid": "m2", "threadId": "thread_other"}]
        return original_search(query, max_results=max_results)

    gmail.search_emails = _patched_search

    result = tool.execute(
        _msg(
            {
                "participant_email": "mark@example.com",
                "account_id": "google_emi",
                "lookback_hours": 10,
                "include_recent_messages": True,
                "recent_message_limit": 3,
            }
        )
    )

    assert result.result_type == "get_email_thread"
    assert isinstance(result.data, dict)
    assert result.data.get("thread_id") == "thread_inbound"
    assert result.data.get("lookback_hours") == 10
