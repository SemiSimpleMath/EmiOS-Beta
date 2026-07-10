"""Twilio signature-bypass gate (inbound-webhooks audit W1, 2026-07-09).

The bypass was gated on dev_tools_enabled(), which returns True whenever
EMI_DEV_TOOLS is set — with NO origin check (its own docstring: "not a
security boundary"). So EMI_DEV_TOOLS=1 + EMI_TWILIO_SKIP_SIG_LOCALHOST=1
disabled X-Twilio-Signature verification for EVERY request, not just
localhost. The gate now uses is_local_request() (loopback AND no
proxy/tunnel headers), so a tunneled/internet request always hits
signature verification even with the skip flag set.
"""
from __future__ import annotations

import pytest
from flask import Flask

from app.routes.twilio_sms import twilio_sms_bp


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMI_DEV_TOOLS", "1")
    monkeypatch.setenv("EMI_TWILIO_SKIP_SIG_LOCALHOST", "1")
    app = Flask(__name__)
    app.register_blueprint(twilio_sms_bp)
    return app.test_client()


def test_tunneled_request_still_hits_signature_verification(client, monkeypatch):
    """A request carrying a proxy header is NOT local — even with both env
    vars set, the bypass must not apply, so a missing signature is rejected."""
    verified = {"called": False}

    def _fake_verify(_req):
        verified["called"] = True
        return False   # no valid signature

    monkeypatch.setattr("app.routes.twilio_sms._verify_twilio_signature", _fake_verify)

    resp = client.post(
        "/twilio/sms",
        data={"From": "+15551234567", "To": "+15557654321", "Body": "hi", "MessageSid": "SM1"},
        headers={"X-Forwarded-For": "203.0.113.9"},   # tunnel/proxy → not local
    )
    assert verified["called"] is True          # signature check ran
    assert resp.status_code == 403             # and rejected the unsigned request


def test_genuinely_local_request_may_skip_when_opted_in(client, monkeypatch):
    """A loopback request with no proxy headers and the skip flag set does
    bypass verification (the intended localhost-dev affordance)."""
    def _boom(_req):
        raise AssertionError("signature verification should be skipped for a local opted-in request")

    monkeypatch.setattr("app.routes.twilio_sms._verify_twilio_signature", _boom)
    # No allowlist configured -> the request is dropped at the number gate with
    # a 200 TwiML, but crucially WITHOUT hitting signature verification.
    resp = client.post(
        "/twilio/sms",
        data={"From": "+15551234567", "To": "+15557654321", "Body": "hi", "MessageSid": "SM2"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert resp.status_code == 200


def test_skip_flag_off_always_verifies(monkeypatch):
    monkeypatch.setenv("EMI_DEV_TOOLS", "1")
    monkeypatch.delenv("EMI_TWILIO_SKIP_SIG_LOCALHOST", raising=False)
    app = Flask(__name__)
    app.register_blueprint(twilio_sms_bp)
    c = app.test_client()

    called = {"v": False}
    monkeypatch.setattr(
        "app.routes.twilio_sms._verify_twilio_signature",
        lambda _req: called.__setitem__("v", True) or False,
    )
    resp = c.post(
        "/twilio/sms",
        data={"From": "+1", "To": "+2", "Body": "x", "MessageSid": "SM3"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},   # local, but skip flag OFF
    )
    assert called["v"] is True
    assert resp.status_code == 403
