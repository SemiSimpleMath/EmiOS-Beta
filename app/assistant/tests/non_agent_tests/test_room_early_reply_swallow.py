"""Early-return reply-send errors are swallowed on every surface (RSM3, 2026-07-09).

When a slash/plan-mode command short-circuits the pipeline and the early reply
fails to send, the surface services log and swallow rather than re-raise. The
inbound webhooks already returned 200 and their async workers catch exceptions,
so a re-raise only re-logs. Telegram already did this; Slack and UI were
re-raising — now all three are consistent.
"""
from __future__ import annotations

import pytest

from app.assistant.room_session_manager.services.surfaces.slack_inbound_service import SlackInboundService
from app.assistant.room_session_manager.services.surfaces.telegram_inbound_service import TelegramInboundService
from app.assistant.room_session_manager.services.surfaces.ui_inbound_service import UiInboundService


class _RaisingTransport:
    def send_reply(self, **kwargs):
        raise RuntimeError("transport send failed")


class _FakeIngress:
    def apply_room_mode_context(self, **kwargs):
        # Return an early-result dict → drives the early short-circuit path.
        return "", {}, {"reply_text": "early control reply"}


class _FakeManager:
    def __init__(self):
        self.ingress_service = _FakeIngress()
        self.slack_transport = _RaisingTransport()
        self.telegram_transport = _RaisingTransport()
        self.ui_transport = _RaisingTransport()
        self.early_failure_notices = []

    def _derive_room_contact_name(self, room_id):
        return "Contact"

    def _local_timestamp_str(self):
        return "2026-07-09 12:00:00"

    def _build_short_circuit_response(self, **kwargs):
        return {"short_circuit": True, **kwargs}

    def surface_early_reply_failure(self, *, room_id, surface, reply_text, error):
        # The early path's failed send now raises an owner notice (verification B3: the
        # swallow was the ONLY delivery — no durable record, no recovery).
        self.early_failure_notices.append({"room_id": room_id, "surface": surface})


@pytest.fixture(autouse=True)
def _stub_room_ctx(monkeypatch):
    for mod in (
        "app.assistant.room_session_manager.services.surfaces.slack_inbound_service",
        "app.assistant.room_session_manager.services.surfaces.telegram_inbound_service",
        "app.assistant.room_session_manager.services.surfaces.ui_inbound_service",
    ):
        monkeypatch.setattr(f"{mod}.load_room_context_for_manager", lambda room_id: {})
    monkeypatch.setattr(
        "app.assistant.room_session_manager.services.surfaces.ui_inbound_service.get_required_primary_user_name",
        lambda: "User",
    )


def test_slack_early_reply_send_error_is_swallowed():
    mgr = _FakeManager()
    result = SlackInboundService().handle(
        mgr, channel_id="C1", body="/done", room_id="r1", send_reply=True
    )
    assert isinstance(result, dict) and result.get("short_circuit") is True
    assert [n["surface"] for n in mgr.early_failure_notices] == ["slack"]


def test_ui_early_reply_send_error_is_swallowed():
    mgr = _FakeManager()
    result = UiInboundService().handle(
        mgr, socket_id="s1", body="/done", room_id="r1", send_reply=True
    )
    assert isinstance(result, dict) and result.get("short_circuit") is True
    assert [n["surface"] for n in mgr.early_failure_notices] == ["ui"]


def test_telegram_early_reply_send_error_is_swallowed():
    mgr = _FakeManager()
    result = TelegramInboundService().handle(
        mgr, chat_id="123", body="/done", room_id="r1", send_reply=True
    )
    assert isinstance(result, dict) and result.get("short_circuit") is True
    assert [n["surface"] for n in mgr.early_failure_notices] == ["telegram"]


def test_sms_early_reply_send_error_is_swallowed_and_noticed():
    # the RSM3 alignment sweep missed SMS (it still re-raised) — pin the fourth surface too
    import app.assistant.room_session_manager.services.surfaces.sms_inbound_service as sms_mod

    mgr = _FakeManager()
    mgr.sms_transport = _RaisingTransport()
    result = sms_mod.SmsInboundService().handle(
        mgr, from_number="+100", to_number="+200", body="/done", room_id="r1",
        message_sid="SM1", send_reply=True
    )
    assert isinstance(result, dict) and result.get("short_circuit") is True
    assert [n["surface"] for n in mgr.early_failure_notices] == ["sms"]
