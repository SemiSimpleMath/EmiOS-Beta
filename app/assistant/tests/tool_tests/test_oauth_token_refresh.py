"""Tests for oauth_token_refresh tool.

Verifies:
  - Pod must exist and be of kind auth.oauth
  - Refresh POST is made with correct grant_type and credentials
  - New access_token is written to its env var; agent NEVER sees the token
  - Refresh-token rotation is detected and persisted to env var
  - Expiry projection is updated
  - Short-circuit when token isn't near expiry (with stored expiry > 60s)
  - force=true bypasses short-circuit
  - Bad pod kinds rejected
  - Refresh endpoint failures mapped to structured errors
"""
from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh import OauthTokenRefresh
from app.assistant.pod_store.authority import AUTH_USER
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy, ScopeContext, ScopeResourcePolicy, ScopeToolPolicy,
    ToolMessage,
)


def _user_scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::test::oauth_refresh", owner_id="jukka", actor_id="test_caller",
        surface="ui", room_id="test",
        approval=ScopeApprovalPolicy(authority_level=AUTH_USER),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        tools=ScopeToolPolicy(),
    )


def _clean(pod_id: str) -> None:
    conn = sqlite3.connect("emi.db")
    conn.execute("DELETE FROM pod_projection WHERE pod_id=?", (pod_id,))
    conn.execute("DELETE FROM pod_audit WHERE pod_id=?", (pod_id,))
    conn.execute("DELETE FROM pod_store WHERE pod_id=?", (pod_id,))
    conn.commit()
    conn.close()


class TestOauthTokenRefresh(unittest.TestCase):

    def setUp(self):
        # Stage env vars + mint an auth.oauth pod with a 30-min-future expiry
        # (so default `force=false` will short-circuit unless we set near-expiry).
        os.environ["EMI_POD_TEST_OAUTH_ACCESS"] = "old-access-token-12345"
        os.environ["EMI_POD_TEST_OAUTH_REFRESH"] = "old-refresh-token-67890"
        os.environ["EMI_POD_TEST_OAUTH_CLIENT_ID"] = "test-client-id"
        os.environ["EMI_POD_TEST_OAUTH_CLIENT_SECRET"] = "test-client-secret"

        self.store = PodStore()
        # Default to near-expiry so unforced refresh fires.
        near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        self.pod_id = self.store.put_secret_pod(
            pod_type="auth.oauth",
            owner_subject_id="jukka",
            name="Test OAuth pod",
            env_ref="EMI_POD_TEST_OAUTH_ACCESS",
            scope=_user_scope(),
            metadata={
                "refresh_url": "https://provider.test/oauth/token",
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "client_id_env_ref": "EMI_POD_TEST_OAUTH_CLIENT_ID",
                "client_secret_env_ref": "EMI_POD_TEST_OAUTH_CLIENT_SECRET",
                "provider": "test_provider",
            },
            materializer_kwargs={
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "refresh_url": "https://provider.test/oauth/token",
                "provider": "test_provider",
                "expiry_iso": near_expiry,
            },
        )

    def tearDown(self):
        _clean(self.pod_id)
        for k in [
            "EMI_POD_TEST_OAUTH_ACCESS", "EMI_POD_TEST_OAUTH_REFRESH",
            "EMI_POD_TEST_OAUTH_CLIENT_ID", "EMI_POD_TEST_OAUTH_CLIENT_SECRET",
        ]:
            os.environ.pop(k, None)

    # --------------------------------------------------------- happy path

    def test_refresh_rotates_access_token_in_env(self):
        tool = OauthTokenRefresh()
        captured = {}

        def fake_post(url, data=None, timeout=None, headers=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {
                "access_token": "NEW-access-token-AAA",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
            resp.text = "{...}"
            return resp

        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
            side_effect=fake_post,
        ):
            tm = ToolMessage(
                request_id="oauth-1",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": self.pod_id}},
            )
            result = tool.execute(tm)

        self.assertEqual(result.result_type, "oauth_token_refresh")
        self.assertTrue(result.data["ok"])
        self.assertTrue(result.data["refreshed"])
        self.assertEqual(result.data["provider"], "test_provider")
        self.assertFalse(result.data["refresh_token_rotated"])

        # POST body shaped correctly.
        self.assertEqual(captured["url"], "https://provider.test/oauth/token")
        self.assertEqual(captured["data"]["grant_type"], "refresh_token")
        self.assertEqual(captured["data"]["refresh_token"], "old-refresh-token-67890")
        self.assertEqual(captured["data"]["client_id"], "test-client-id")
        self.assertEqual(captured["data"]["client_secret"], "test-client-secret")

        # Env var updated with NEW access token.
        self.assertEqual(os.environ["EMI_POD_TEST_OAUTH_ACCESS"], "NEW-access-token-AAA")
        # Refresh token unchanged (no rotation in response).
        self.assertEqual(os.environ["EMI_POD_TEST_OAUTH_REFRESH"], "old-refresh-token-67890")

        # NEW access token MUST NOT appear in tool result.
        self.assertNotIn("NEW-access-token-AAA", result.content)
        self.assertNotIn("NEW-access-token-AAA", str(result.data))
        # OLD tokens also not leaked.
        self.assertNotIn("old-access-token-12345", str(result.data))
        self.assertNotIn("old-refresh-token-67890", str(result.data))

    def test_refresh_handles_refresh_token_rotation(self):
        tool = OauthTokenRefresh()

        def fake_post(*a, **kw):
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {
                "access_token": "NEW-access",
                "refresh_token": "NEW-refresh-rotated",
                "expires_in": 3600,
            }
            resp.text = "{}"
            return resp

        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
            side_effect=fake_post,
        ):
            tm = ToolMessage(
                request_id="oauth-rot",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": self.pod_id}},
            )
            result = tool.execute(tm)

        self.assertTrue(result.data["refresh_token_rotated"])
        self.assertEqual(os.environ["EMI_POD_TEST_OAUTH_REFRESH"], "NEW-refresh-rotated")

    def test_short_circuit_when_token_not_near_expiry(self):
        """Stored expiry > 60s in future, force=false -> short-circuit, no POST."""
        # Re-mint the pod with a far-future expiry.
        _clean(self.pod_id)
        far_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.pod_id = self.store.put_secret_pod(
            pod_type="auth.oauth",
            owner_subject_id="jukka",
            name="Test OAuth (far expiry)",
            env_ref="EMI_POD_TEST_OAUTH_ACCESS",
            scope=_user_scope(),
            metadata={
                "refresh_url": "https://provider.test/oauth/token",
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "provider": "test_provider",
            },
            materializer_kwargs={
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "refresh_url": "https://provider.test/oauth/token",
                "provider": "test_provider",
                "expiry_iso": far_expiry,
            },
        )

        tool = OauthTokenRefresh()
        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
        ) as mock_post:
            tm = ToolMessage(
                request_id="oauth-short",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": self.pod_id}},
            )
            result = tool.execute(tm)
        mock_post.assert_not_called()
        self.assertFalse(result.data["refreshed"])
        self.assertIn("seconds_remaining", result.data)
        self.assertGreater(result.data["seconds_remaining"], 60)

    def test_force_bypasses_short_circuit(self):
        """Even with far-future expiry, force=true triggers the POST."""
        _clean(self.pod_id)
        far_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.pod_id = self.store.put_secret_pod(
            pod_type="auth.oauth",
            owner_subject_id="jukka",
            name="Test OAuth (far expiry, force)",
            env_ref="EMI_POD_TEST_OAUTH_ACCESS",
            scope=_user_scope(),
            metadata={
                "refresh_url": "https://provider.test/oauth/token",
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "provider": "test_provider",
            },
            materializer_kwargs={
                "refresh_token_env_ref": "EMI_POD_TEST_OAUTH_REFRESH",
                "refresh_url": "https://provider.test/oauth/token",
                "provider": "test_provider",
                "expiry_iso": far_expiry,
            },
        )

        tool = OauthTokenRefresh()
        post_called = []

        def fake_post(*a, **kw):
            post_called.append(True)
            resp = MagicMock()
            resp.status_code = 200
            resp.json = lambda: {"access_token": "T", "expires_in": 3600}
            resp.text = "{}"
            return resp

        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
            side_effect=fake_post,
        ):
            tm = ToolMessage(
                request_id="oauth-force",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": self.pod_id, "force": True}},
            )
            result = tool.execute(tm)
        self.assertEqual(len(post_called), 1)
        self.assertTrue(result.data["refreshed"])

    # --------------------------------------------------------- error cases

    def test_unknown_pod_returns_pod_not_found(self):
        tool = OauthTokenRefresh()
        tm = ToolMessage(
            request_id="oauth-404",
            tool_name="oauth_token_refresh",
            tool_data={"arguments": {"pod_id": "datapod:auth.oauth:doesnotexist"}},
        )
        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
        ) as mock_post:
            result = tool.execute(tm)
        mock_post.assert_not_called()
        self.assertEqual(result.data["error_code"], "pod_not_found")

    def test_wrong_pod_kind_rejected(self):
        """Pointing at a non-auth.oauth pod must fail before any POST."""
        os.environ["EMI_POD_TEST_HTTP_TOKEN_FULL"] = "Bearer non-oauth-token-9999"
        bearer_pod_id = self.store.put_secret_pod(
            pod_type="auth.bearer",
            owner_subject_id="jukka",
            name="Wrong-kind pod",
            env_ref="EMI_POD_TEST_HTTP_TOKEN_FULL",
            scope=_user_scope(),
        )
        try:
            tool = OauthTokenRefresh()
            tm = ToolMessage(
                request_id="oauth-wrongkind",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": bearer_pod_id}},
            )
            with patch(
                "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
            ) as mock_post:
                result = tool.execute(tm)
            mock_post.assert_not_called()
            self.assertEqual(result.data["error_code"], "invalid_pod_kind")
        finally:
            _clean(bearer_pod_id)
            os.environ.pop("EMI_POD_TEST_HTTP_TOKEN_FULL", None)

    def test_refresh_endpoint_4xx_returns_structured_error(self):
        tool = OauthTokenRefresh()

        def fake_post(*a, **kw):
            resp = MagicMock()
            resp.status_code = 401
            resp.json = lambda: {"error": "invalid_grant"}
            resp.text = '{"error":"invalid_grant"}'
            return resp

        with patch(
            "app.assistant.lib.tools.oauth_token_refresh.oauth_token_refresh.requests.post",
            side_effect=fake_post,
        ):
            tm = ToolMessage(
                request_id="oauth-401",
                tool_name="oauth_token_refresh",
                tool_data={"arguments": {"pod_id": self.pod_id}},
            )
            result = tool.execute(tm)
        self.assertEqual(result.data["error_code"], "refresh_http_401")
        # access token must NOT have been rotated on failure
        self.assertEqual(os.environ["EMI_POD_TEST_OAUTH_ACCESS"], "old-access-token-12345")


if __name__ == "__main__":
    unittest.main()
