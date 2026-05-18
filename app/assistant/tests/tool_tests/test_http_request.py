"""Tests for http_request — pod-aware HTTP/REST tool.

Verifies:
  - Pod refs in header values are resolved at courier scope
  - Resolved auth tokens DO NOT appear in the tool result
  - Real network call is mocked; we inspect the request that requests would
    have made
  - http_audit row is written
  - response_pod_kind returns not_implemented in v1
  - Network errors and HTTP error statuses map to the right error codes
  - expect_status enforcement
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.lib.tools.http_request.http_request import HttpRequest
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeToolPolicy,
    ToolMessage,
)


def _user_scope() -> ScopeContext:
    from app.assistant.pod_store.authority import AUTH_USER
    return ScopeContext(
        scope_id="scope::test::http_request_user",
        owner_id="jukka", actor_id="test_caller",
        surface="ui", room_id="test",
        approval=ScopeApprovalPolicy(authority_level=AUTH_USER),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        tools=ScopeToolPolicy(),
    )


def _clean_pod(pod_id: str) -> None:
    conn = sqlite3.connect("emi.db")
    conn.execute("DELETE FROM pod_projection WHERE pod_id=?", (pod_id,))
    conn.execute("DELETE FROM pod_audit WHERE pod_id=?", (pod_id,))
    conn.execute("DELETE FROM pod_store WHERE pod_id=?", (pod_id,))
    conn.commit()
    conn.close()


class TestHttpRequest(unittest.TestCase):
    """Pod-resolution + audit + error-mapping tests. requests is mocked."""

    def setUp(self):
        # Stage a real bearer-token pod (auth.bearer kind) backed by an env
        # var. Mirrors how a power user would create a WHOOP / Spotify /
        # GitHub PAT pod for the http_request tool to consume.
        os.environ["EMI_POD_TEST_HTTP_TOKEN_FULL"] = "Bearer test-token-xyz-1234"
        self.store = PodStore()
        self.pod_id = self.store.put_secret_pod(
            pod_type="auth.bearer",
            owner_subject_id="jukka",
            name="Test HTTP Auth Token",
            env_ref="EMI_POD_TEST_HTTP_TOKEN_FULL",
            scope=_user_scope(),
        )

    def tearDown(self):
        _clean_pod(self.pod_id)
        os.environ.pop("EMI_POD_TEST_HTTP_TOKEN_FULL", None)

    # --------------------------------------------------------- pod resolution

    def test_pod_ref_in_header_resolved_at_courier_value_not_in_result(self):
        """The pod ref `datapod:auth.bearer:<id>` in an Authorization
        header is resolved to the real token value, the token is passed
        to requests.request, and the token NEVER appears in the tool result."""
        tool = HttpRequest()

        captured = {}

        def fake_request(*args, **kwargs):
            captured["method"] = kwargs.get("method") or (args[0] if args else None)
            captured["url"] = kwargs.get("url") or (args[1] if len(args) > 1 else None)
            captured["headers"] = kwargs.get("headers")
            response = MagicMock()
            response.status_code = 200
            response.content = b'{"ok":true,"data":[1,2,3]}'
            response.text = '{"ok":true,"data":[1,2,3]}'
            response.headers = {"Content-Type": "application/json"}
            response.json = lambda: {"ok": True, "data": [1, 2, 3]}
            return response

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-1",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://api.example.test/v1/stuff",
                        "method": "GET",
                        "headers": {
                            "Authorization": f"datapod:auth.bearer:{self.pod_id.split(':')[-1]}/full",
                            "Accept": "application/json",
                        },
                    }
                },
            )
            result = tool.execute(tm)

        # SUCCESS path
        self.assertEqual(result.result_type, "http_request")
        self.assertEqual(result.data["status"], 200)
        self.assertEqual(result.data["pods_used_count"], 1)

        # Token MUST appear in the headers passed to requests.request
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-token-xyz-1234")
        # Plain header values pass through unchanged
        self.assertEqual(captured["headers"]["Accept"], "application/json")

        # Token MUST NOT appear anywhere in the tool result
        self.assertNotIn("Bearer test-token-xyz-1234", result.content)
        self.assertNotIn("Bearer test-token-xyz-1234", str(result.data))

    def test_plain_headers_no_pod_resolution_needed(self):
        """No `datapod:` ref → no pod resolution path triggered."""
        tool = HttpRequest()
        captured = {}

        def fake_request(*args, **kwargs):
            captured["headers"] = kwargs.get("headers")
            response = MagicMock()
            response.status_code = 200
            response.content = b'{"ok":true}'
            response.text = '{"ok":true}'
            response.headers = {"Content-Type": "application/json"}
            response.json = lambda: {"ok": True}
            return response

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-2",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
                        "headers": {"User-Agent": "Emi/1.0"},
                    }
                },
            )
            result = tool.execute(tm)
        self.assertEqual(result.data["pods_used_count"], 0)
        self.assertEqual(captured["headers"]["User-Agent"], "Emi/1.0")

    # --------------------------------------------------------- error mapping

    def test_invalid_pod_ref_returns_structured_error(self):
        tool = HttpRequest()
        tm = ToolMessage(
            request_id="req-http-3",
            tool_name="http_request",
            tool_data={
                "arguments": {
                    "url": "https://api.example.test/x",
                    "headers": {"Authorization": "datapod:malformed"},
                }
            },
        )
        # MUST NOT make an actual HTTP request when pod resolution fails
        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
        ) as mock_req:
            result = tool.execute(tm)
        mock_req.assert_not_called()
        self.assertEqual(result.data.get("error_code"), "invalid_pod_ref")

    def test_missing_pod_returns_pod_not_found(self):
        tool = HttpRequest()
        tm = ToolMessage(
            request_id="req-http-4",
            tool_name="http_request",
            tool_data={
                "arguments": {
                    "url": "https://api.example.test/x",
                    "headers": {"Authorization": "datapod:auth.bearer:doesnotexist/full"},
                }
            },
        )
        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
        ) as mock_req:
            result = tool.execute(tm)
        mock_req.assert_not_called()
        self.assertEqual(result.data.get("error_code"), "pod_not_found")

    def test_response_pod_kind_seals_response_into_new_pod(self):
        """v1.1: setting response_pod_kind seals the response body into a new
        pod. The agent receives only pod_id + metadata; body content is
        absent from the tool result."""
        tool = HttpRequest()

        secret_body = '{"heart_rate":78,"sleep_score":91,"strain":13.4}'

        def fake_request(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.content = secret_body.encode()
            response.text = secret_body
            response.headers = {"Content-Type": "application/json"}
            response.json = lambda: {"heart_rate": 78, "sleep_score": 91, "strain": 13.4}
            return response

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-seal",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://api.prod.whoop.example/v2/cycle",
                        "response_pod_kind": "health.private",
                    }
                },
            )
            result = tool.execute(tm)

        self.assertEqual(result.result_type, "http_request")
        self.assertEqual(result.data["status"], 200)

        # The agent gets a pod_id, not the body.
        response_pod_id = result.data.get("response_pod_id")
        self.assertIsNotNone(response_pod_id)
        self.assertTrue(response_pod_id.startswith("datapod:health.private:"))
        self.assertEqual(result.data["response_pod_kind"], "health.private")
        # health.private → AUTH_USER (99)
        self.assertEqual(result.data["response_pod_min_authority"], 99)

        # The body must NOT appear in the tool result.
        self.assertNotIn(secret_body, result.content)
        self.assertNotIn(secret_body, str(result.data))
        self.assertNotIn("heart_rate", str(result.data))
        # body / body_json absent in sealed mode.
        self.assertNotIn("body", result.data)
        self.assertNotIn("body_json", result.data)

        # The actual response body lives in the new pod's body, fetchable
        # only at the declared authority.
        conn = sqlite3.connect("emi.db")
        c = conn.cursor()
        c.execute(
            "SELECT kind, min_authority, body FROM pod_store WHERE pod_id=?",
            (response_pod_id,),
        )
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        kind, min_auth, body = row
        self.assertEqual(kind, "health.private")
        self.assertEqual(min_auth, 99)
        self.assertEqual(body, secret_body)

        # Cleanup the test pod
        conn = sqlite3.connect("emi.db")
        conn.execute("DELETE FROM pod_store WHERE pod_id=?", (response_pod_id,))
        conn.commit()
        conn.close()

    def test_response_pod_kind_authority_mapping(self):
        """Suffix-based authority mapping for response pod kinds."""
        from app.assistant.lib.tools.http_request.http_request import HttpRequest as HR
        cases = [
            ("health.private", 99),
            ("financial.private", 99),
            ("user.gated", 70),
            ("internal.chat", 50),
            ("ops.protected", 50),
            ("docs.public", 10),
            ("totally_unknown_suffix", 99),  # fail-closed default
            ("noseparator", 99),  # no '.' = entire string is "suffix" = unknown
        ]
        for kind, expected in cases:
            with self.subTest(kind=kind):
                self.assertEqual(HR._kind_to_min_authority(kind), expected)

    def test_expect_status_failure_returns_http_error_code(self):
        tool = HttpRequest()

        def fake_request(*args, **kwargs):
            response = MagicMock()
            response.status_code = 404
            response.content = b'{"error":"not found"}'
            response.text = '{"error":"not found"}'
            response.headers = {"Content-Type": "application/json"}
            response.json = lambda: {"error": "not found"}
            return response

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-6",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://api.example.test/missing",
                        "expect_status": [200, 201],
                    }
                },
            )
            result = tool.execute(tm)
        self.assertEqual(result.data.get("error_code"), "http_404")

    def test_timeout_returns_network_timeout(self):
        tool = HttpRequest()
        import requests

        def fake_request(*args, **kwargs):
            raise requests.exceptions.Timeout("timed out")

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-7",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://api.example.test/slow",
                        "timeout_s": 0.1,
                    }
                },
            )
            result = tool.execute(tm)
        self.assertEqual(result.data.get("error_code"), "network_timeout")

    # --------------------------------------------------------- audit

    def test_audit_row_written_on_success(self):
        tool = HttpRequest()

        def fake_request(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.content = b'{"ok":true}'
            response.text = '{"ok":true}'
            response.headers = {"Content-Type": "application/json"}
            response.json = lambda: {"ok": True}
            return response

        with patch(
            "app.assistant.lib.tools.http_request.http_request.requests.request",
            side_effect=fake_request,
        ):
            tm = ToolMessage(
                request_id="req-http-audit-1",
                tool_name="http_request",
                tool_data={
                    "arguments": {
                        "url": "https://api.example.test/audit-test",
                        "method": "GET",
                    }
                },
            )
            tool.execute(tm)

        # Verify audit row
        conn = sqlite3.connect("emi.db")
        c = conn.cursor()
        c.execute(
            "SELECT method, url_host, url_path, status_code, error_code "
            "FROM http_audit WHERE request_id=? ORDER BY id DESC LIMIT 1",
            ("req-http-audit-1",),
        )
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row, "http_audit row should be written on success")
        method, host, path, status, error_code = row
        self.assertEqual(method, "GET")
        self.assertEqual(host, "api.example.test")
        self.assertEqual(path, "/audit-test")
        self.assertEqual(status, 200)
        self.assertIsNone(error_code)


if __name__ == "__main__":
    unittest.main()
