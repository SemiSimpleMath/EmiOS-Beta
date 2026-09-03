"""send_email.from_account: the closed vocabulary lives in the FORM (2026-09-02).

The planner reads the compact tool card, not the alias doctrine, and once filled
this field with the user's raw email address off an entity card. Any string
type-validates, so the guess rode the tool-args fast path (which skips the args
agent — the one reader of the full documentation), reached the credentials
layer, and popped an OAuth consent flow the registry gate had to block.

The vocabulary is now a @field_validator on the form. That makes the fast-path
gate itself the router: a wrong value fails model_validate, the gate returns
False, and the call drops to the slow path where the args agent corrects it in
the same cycle. Nothing else changed — the escape hatch existed; it needed
something to trip on.

Hermetic: the form module is loaded by path; no DI, no database.
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

_PATH = "app/assistant/lib/tools/send_email/tool_forms/tool_forms.py"
_spec = importlib.util.spec_from_file_location("send_email_forms_under_test", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["send_email_forms_under_test"] = _mod
_spec.loader.exec_module(_mod)


def _validate(**kw):
    return _mod.send_email_args.model_validate(
        {"to": "someone@example.com", "subject": "s", "body": "b", **kw})


class TestFromAccountVocabulary:
    def test_omitted_and_empty_pass(self):
        assert _validate().from_account is None
        assert _validate(from_account="").from_account == ""

    def test_aliases_pass(self):
        assert _validate(from_account="self").from_account == "self"
        assert _validate(from_account="user").from_account == "user"

    def test_raw_email_address_is_rejected(self):
        """The 2026-09-02 incident: the user's own address is knowledge, not an
        account id — it must fail the form so the args agent gets the call."""
        with pytest.raises(Exception, match="raw email address"):
            _validate(from_account="user-own-address@example.com")

    def test_unknown_account_is_rejected_with_the_vocabulary(self):
        with pytest.raises(Exception, match="'self', 'user'"):
            _validate(from_account="not_a_real_account")

    def test_registry_account_passes(self, monkeypatch):
        """A known registry id is the documented 'advanced' path. The registry is
        substituted — a unit test never reads the per-user oauth config."""
        from app.assistant.lib.google_auth import oauth_registry
        monkeypatch.setattr(oauth_registry, "is_known_account",
                            lambda v: v == "google_user_primary")
        assert _validate(from_account="google_user_primary").from_account == "google_user_primary"

    def test_envelope_form_carries_the_same_wall(self):
        """The fast-path gate validates the ENVELOPE (tool_name + arguments) —
        the nested form must reject through it identically."""
        with pytest.raises(Exception, match="raw email address"):
            _mod.send_email_arguments.model_validate({
                "tool_name": "send_email",
                "arguments": {"to": "a@b.com", "subject": "s", "body": "b",
                              "from_account": "someone@somewhere.com"},
            })
