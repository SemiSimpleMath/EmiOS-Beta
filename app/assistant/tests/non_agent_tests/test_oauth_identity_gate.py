"""A consent window must say which identity it wants, and refuse the wrong one.

2026-08-24: `emi_google_primary` ("the assistant's Google account") sat consented as
the OWNER's Gmail for months. Two gaps caused it: the auto-opened consent tab named
only an opaque account_id, so the human could not know which Google identity to sign
in as; and the callback stored whatever came back without checking. Reads kept
succeeding (fetch paths don't assert identity), so the mis-consent stayed invisible
until the token expired and prompted again — "it asks and never takes".

The flow now resolves the expected identity from the env registry, passes it to Google
as `login_hint` so the chooser pre-selects it, and gates the callback on a definite
mismatch.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.routes.google_oauth import (
    _google_auth_kwargs,
    _identity_mismatch,
)


class TestIdentityMismatch:

    def test_definite_mismatch_blocks(self):
        assert _identity_mismatch("her@gmail.com", "his@gmail.com") is True

    def test_case_and_whitespace_insensitive_match_passes(self):
        assert _identity_mismatch("Her@Gmail.com", "  her@gmail.com ") is False

    def test_unclaimed_account_never_blocks(self):
        # No recorded expectation -> we do not invent one.
        assert _identity_mismatch("", "anyone@gmail.com") is False

    def test_unfetchable_principal_never_blocks(self):
        # e.g. the nest account: no gmail scope, so no principal to compare.
        assert _identity_mismatch("her@gmail.com", None) is False
        assert _identity_mismatch("her@gmail.com", "") is False


class TestAuthKwargs:

    def test_always_requests_refresh_token_and_account_chooser(self):
        kw = _google_auth_kwargs("google_nest")
        assert kw["access_type"] == "offline"          # refresh token, not just access
        assert "select_account" in kw["prompt"]        # let the human pick the account

    def test_login_hint_present_when_identity_is_known(self, monkeypatch):
        import app.routes.google_oauth as go
        monkeypatch.setattr(go, "_expected_identity", lambda aid: "her@gmail.com")
        assert _google_auth_kwargs("emi_google_primary")["login_hint"] == "her@gmail.com"

    def test_no_login_hint_when_identity_unknown(self, monkeypatch):
        import app.routes.google_oauth as go
        monkeypatch.setattr(go, "_expected_identity", lambda aid: "")
        assert "login_hint" not in _google_auth_kwargs("google_user_primary")
