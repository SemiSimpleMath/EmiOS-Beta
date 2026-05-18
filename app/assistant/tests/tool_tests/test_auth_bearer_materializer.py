"""Tests for the auth.bearer pod materializer.

Verifies:
  - Projections are produced at expected authority bands
  - Validation rejects empty / too-short / too-long values
  - Round-trip through PodStore.put_secret_pod + fetch_projection works
  - Full projection is courier-only; redacted/prefix are chat/public
"""
from __future__ import annotations

import os
import sqlite3
import unittest

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_COURIER, PodAuthorityError,
)
from app.assistant.pod_store.materializers.auth_bearer import materialize
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy, ScopeContext, ScopeResourcePolicy, ScopeToolPolicy,
)


def _scope(authority: int) -> ScopeContext:
    return ScopeContext(
        scope_id=f"scope::test::auth_bearer_{authority}",
        owner_id="jukka", actor_id="test_caller",
        surface="ui", room_id="test",
        approval=ScopeApprovalPolicy(authority_level=authority),
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


class TestAuthBearerMaterializer(unittest.TestCase):

    # --------------------------------------------------------- direct calls

    def test_materialize_produces_four_projections(self):
        specs = materialize(
            raw_value="ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
            env_ref="EMI_POD_TEST_BEARER",
        )
        names = [s.projection_name for s in specs]
        self.assertEqual(set(names), {"full", "prefix", "redacted", "format"})

    def test_full_projection_is_courier_only_and_uses_env_ref(self):
        specs = materialize(
            raw_value="ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ",
            env_ref="EMI_POD_TEST_BEARER",
        )
        full = next(s for s in specs if s.projection_name == "full")
        self.assertEqual(full.min_authority, AUTH_COURIER)
        self.assertEqual(full.storage_kind, "env")
        self.assertEqual(full.env_ref, "EMI_POD_TEST_BEARER")
        self.assertIsNone(full.plain_value)

    def test_prefix_projection_shows_first_six_chars_plus_ellipsis(self):
        specs = materialize(
            raw_value="ghp_aBcDeFgHiJkLmNoPqRs",
            env_ref="EMI_POD_TEST_BEARER",
        )
        prefix = next(s for s in specs if s.projection_name == "prefix")
        self.assertEqual(prefix.plain_value, "ghp_aB...")
        self.assertEqual(prefix.min_authority, AUTH_CHAT)

    def test_redacted_is_fixed_stars_at_public_authority(self):
        specs = materialize(
            raw_value="ghp_aBcDeFgHiJkLmNoPqRs",
            env_ref="EMI_POD_TEST_BEARER",
        )
        redacted = next(s for s in specs if s.projection_name == "redacted")
        self.assertEqual(redacted.plain_value, "***")
        self.assertEqual(redacted.min_authority, AUTH_PUBLIC)

    def test_too_short_rejected(self):
        with self.assertRaises(ValueError):
            materialize(raw_value="abc", env_ref="EMI_POD_TEST_BEARER")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            materialize(raw_value="", env_ref="EMI_POD_TEST_BEARER")

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            materialize(raw_value="x" * 5000, env_ref="EMI_POD_TEST_BEARER")

    # --------------------------------------------------------- round-trip

    def test_round_trip_through_pod_store(self):
        """End-to-end: put_secret_pod creates projections; fetch at courier
        scope returns the actual token; fetch at chat scope returns prefix only."""
        os.environ["EMI_POD_TEST_BEARER_RT"] = "ghp_thisIsATestTokenForRoundTrip"
        store = PodStore()
        pod_id = store.put_secret_pod(
            pod_type="auth.bearer",
            owner_subject_id="jukka",
            name="Test auth bearer pod",
            env_ref="EMI_POD_TEST_BEARER_RT",
            scope=_scope(AUTH_COURIER),
        )
        try:
            # Courier can fetch the full token.
            full_value = store.fetch_projection(
                pod_id, "full", scope=_scope(AUTH_COURIER),
            )
            self.assertEqual(full_value, "ghp_thisIsATestTokenForRoundTrip")

            # Chat-tier scope CANNOT fetch full.
            with self.assertRaises(PodAuthorityError):
                store.fetch_projection(
                    pod_id, "full", scope=_scope(AUTH_CHAT),
                )

            # Chat-tier scope CAN fetch prefix.
            prefix = store.fetch_projection(
                pod_id, "prefix", scope=_scope(AUTH_CHAT),
            )
            self.assertEqual(prefix, "ghp_th...")

            # Public scope CAN fetch redacted.
            redacted = store.fetch_projection(
                pod_id, "redacted", scope=_scope(AUTH_PUBLIC),
            )
            self.assertEqual(redacted, "***")
        finally:
            _clean(pod_id)
            os.environ.pop("EMI_POD_TEST_BEARER_RT", None)


if __name__ == "__main__":
    unittest.main()
