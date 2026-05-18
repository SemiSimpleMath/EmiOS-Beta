"""Unit test for web_type_secret — verifies the tool resolves the pod
via courier scope and submits the resolved value to the MCP browser_type
path WITHOUT leaking the value into the tool result.

Does NOT actually launch a browser or hit the real Playwright MCP —
the MCP call is mocked. The DB-side pod lookup is real (uses
PodStore + a temporary identity.ssn pod) so we exercise the full
authority gate.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.tools.web_type_secret.web_type_secret import WebTypeSecret
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.authority import AUTH_USER, AUTH_COURIER
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    ScopeContext,
    ScopeApprovalPolicy,
    ScopeResourcePolicy,
    ScopeToolPolicy,
)


def _user_scope() -> ScopeContext:
    return ScopeContext(
        scope_id="test::user", owner_id="jukka", actor_id="test_caller",
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


class TestWebTypeSecret(unittest.TestCase):
    def setUp(self):
        # Stage a real identity.ssn pod in the live DB. The materializer
        # reads the env var and writes projections; cleanup tears it down.
        os.environ["EMI_POD_TEST_WEB_SSN_FULL"] = "987-65-4321"
        self.store = PodStore()
        self.pod_id = self.store.put_secret_pod(
            pod_type="identity.ssn",
            owner_subject_id="jukka",
            name="Test SSN",
            env_ref="EMI_POD_TEST_WEB_SSN_FULL",
            scope=_user_scope(),
        )

    def tearDown(self):
        _clean_pod(self.pod_id)
        os.environ.pop("EMI_POD_TEST_WEB_SSN_FULL", None)

    def test_value_resolves_via_courier_and_is_typed(self):
        """End-to-end: tool resolves pod at courier scope, hands value to
        MCP browser_type, returns metadata only. Verifies the value
        landed in the MCP arguments AND that it's nowhere in the tool result."""
        tool = WebTypeSecret()

        # Mock the MCP call so no real browser is needed; capture what
        # arguments would have been passed.
        captured_args = {}
        def fake_mcp_call(server_entry, tool_name, arguments, timeout_s):
            captured_args["tool_name"] = tool_name
            captured_args["arguments"] = arguments
            return {"result": {"content": [{"type": "text", "text": "ok"}], "isError": False}}

        # Mock DI's get_mcp_server_entry to return a stub server
        stub_server = {"policy": {"call_timeout_seconds": 20}}
        with patch(
            "app.assistant.lib.tools.web_type_secret.web_type_secret.mcp_stdio_call_tool",
            side_effect=fake_mcp_call,
        ), patch(
            "app.assistant.lib.tools.web_type_secret.web_type_secret.DI"
        ) as di_mock:
            di_mock.tool_registry.get_mcp_server_entry.return_value = stub_server
            tm = ToolMessage(
                request_id="req-1",
                tool_name="web_type_secret",
                tool_data={
                    "arguments": {
                        "pod_id": self.pod_id,
                        "projection": "full",
                        "ref": "e3",
                        "element": "SSN textbox",
                        "submit": False,
                    }
                },
            )
            result = tool.execute(tm)

        # Tool result: SUCCESS, no value
        self.assertEqual(result.result_type, "web_type_secret")
        self.assertIn("typed_length", result.data)
        self.assertEqual(result.data["typed_length"], 11)
        self.assertEqual(result.data["pod_id"], self.pod_id)
        self.assertEqual(result.data["projection"], "full")
        # Value must NOT appear anywhere in the tool result
        self.assertNotIn("987-65-4321", result.content)
        self.assertNotIn("987-65-4321", str(result.data))

        # MCP got the value — exactly once, in the text argument.
        # `target` is playwright-mcp's snapshot-ref parameter name (it
        # was renamed from `ref` in a 2026 upgrade).
        self.assertEqual(captured_args["tool_name"], "browser_type")
        self.assertEqual(captured_args["arguments"]["text"], "987-65-4321")
        self.assertEqual(captured_args["arguments"]["target"], "e3")
        self.assertFalse(captured_args["arguments"]["submit"])

    def test_missing_pod_returns_error(self):
        tool = WebTypeSecret()
        tm = ToolMessage(
            request_id="req-2",
            tool_name="web_type_secret",
            tool_data={
                "arguments": {
                    "pod_id": "datapod:identity.ssn:doesnotexist",
                    "projection": "full",
                    "ref": "e3",
                }
            },
        )
        # No MCP call should happen; failure short-circuits at pod lookup.
        with patch("app.assistant.lib.tools.web_type_secret.web_type_secret.mcp_stdio_call_tool") as mcp_mock:
            result = tool.execute(tm)
        self.assertEqual(result.data.get("error_code"), "pod_not_found")
        mcp_mock.assert_not_called()

    def test_audit_row_written(self):
        tool = WebTypeSecret()
        stub_server = {"policy": {"call_timeout_seconds": 20}}
        with patch(
            "app.assistant.lib.tools.web_type_secret.web_type_secret.mcp_stdio_call_tool",
            return_value={"result": {"content": [{"type": "text", "text": "ok"}], "isError": False}},
        ), patch(
            "app.assistant.lib.tools.web_type_secret.web_type_secret.DI"
        ) as di_mock:
            di_mock.tool_registry.get_mcp_server_entry.return_value = stub_server
            tm = ToolMessage(
                request_id="req-3",
                tool_name="web_type_secret",
                tool_data={
                    "arguments": {
                        "pod_id": self.pod_id,
                        "projection": "full",
                        "ref": "e3",
                    }
                },
            )
            tool.execute(tm)

        # Verify audit recorded the fetch
        conn = sqlite3.connect("emi.db")
        c = conn.cursor()
        c.execute(
            "SELECT operation, outcome, projection_name, caller_scope_authority, detail "
            "FROM pod_audit WHERE pod_id=? AND operation='fetch' ORDER BY created_at DESC LIMIT 1",
            (self.pod_id,),
        )
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        op, outcome, proj, auth, detail = row
        self.assertEqual(outcome, "allowed")
        self.assertEqual(proj, "full")
        self.assertEqual(auth, AUTH_COURIER)
        # Audit detail records storage kind + env ref, not the value
        self.assertIn("storage=env", detail)
        self.assertIn("EMI_POD_TEST_WEB_SSN_FULL", detail)
        self.assertNotIn("987-65-4321", detail)


if __name__ == "__main__":
    unittest.main()
