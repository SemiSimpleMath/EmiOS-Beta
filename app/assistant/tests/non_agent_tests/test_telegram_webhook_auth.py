"""Telegram secret-token verification (inbound-webhooks audit W2, 2026-07-09).

The secret-token check used a plain != (short-circuits on the first
differing byte, leaks length — a timing side-channel). It now uses
hmac.compare_digest, matching the Slack signature path. Behavior is
otherwise unchanged: a wrong/missing token is 401, the right one passes
the auth gate.
"""
from __future__ import annotations

import pytest
from flask import Flask

from app.routes.telegram_webhook import telegram_webhook_bp


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cr3t-token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHAT_IDS", "")   # not reached in these tests
    app = Flask(__name__)
    app.register_blueprint(telegram_webhook_bp)
    return app.test_client()


def test_wrong_token_is_unauthorized(client):
    resp = client.post(
        "/telegram/webhook",
        json={"update_id": 1, "message": {"text": "hi", "chat": {"id": 5}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_missing_token_is_unauthorized(client):
    resp = client.post(
        "/telegram/webhook",
        json={"update_id": 2, "message": {"text": "hi", "chat": {"id": 5}}},
    )
    assert resp.status_code == 401


def test_correct_token_passes_auth_then_drops_at_chat_allowlist(client):
    # Right token clears the secret gate; the empty chat allowlist then drops
    # it with a 200 (fail-closed) — proving auth passed without a 401.
    resp = client.post(
        "/telegram/webhook",
        json={"update_id": 3, "message": {"text": "hi", "chat": {"id": 999}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t-token"},
    )
    assert resp.status_code == 200


def test_compare_is_constant_time():
    import inspect

    from app.routes import telegram_webhook

    src = inspect.getsource(telegram_webhook.telegram_webhook)
    assert "hmac.compare_digest" in src
    assert "header_token != expected" not in src
