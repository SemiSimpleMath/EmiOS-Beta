"""Tests for OutboundChatPublisher — surface routing for chat output."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.assistant.chat_outbound import OutboundChatPublisher


@pytest.fixture
def publisher():
    return OutboundChatPublisher()


# ── No reply_to / empty text ──────────────────────────────────────


class TestEmptyInputs:

    def test_empty_text_returns_false(self, publisher):
        assert publisher.publish(sender="X", text="", reply_to={"type": "socketio"}) is False

    def test_whitespace_text_returns_false(self, publisher):
        assert publisher.publish(sender="X", text="   ", reply_to={"type": "socketio"}) is False

    def test_no_reply_to_returns_false(self, publisher):
        assert publisher.publish(sender="X", text="hi", reply_to=None) is False


# ── Surface routing ───────────────────────────────────────────────


class TestSocketIORouting:

    def test_socketio_publishes_user_message_via_event_hub(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.event_hub.publish = MagicMock()
            ok = publisher.publish(
                sender="Webby",
                text="Looking up the answer",
                reply_to={"type": "socketio", "room_id": "master_room"},
                sub_data_type=["chat_narration"],
            )
        assert ok is True
        # Should have called event_hub.publish exactly once.
        mock_di.event_hub.publish.assert_called_once()
        published = mock_di.event_hub.publish.call_args.args[0]
        assert published.event_topic == "socket_emit"
        assert published.metadata["reply_to"]["room_id"] == "master_room"
        assert published.content == "Looking up the answer"


class TestSlackRouting:

    def test_slack_calls_room_session_manager_slack_transport(self, publisher):
        slack_transport = MagicMock()
        rsm = MagicMock(slack_transport=slack_transport)
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = rsm
            ok = publisher.publish(
                sender="Webby",
                text="On it",
                reply_to={
                    "type": "slack",
                    "channel_id": "C123",
                    "thread_ts": "1234.5",
                    "room_id": "slack::C123",
                },
            )
        assert ok is True
        slack_transport.send_reply.assert_called_once()
        kwargs = slack_transport.send_reply.call_args.kwargs
        assert kwargs["channel_id"] == "C123"
        assert kwargs["thread_ts"] == "1234.5"
        assert kwargs["body"] == "[Webby] On it"

    def test_slack_missing_channel_id_drops(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(slack_transport=MagicMock())
            ok = publisher.publish(
                sender="Webby", text="hi",
                reply_to={"type": "slack", "thread_ts": "1234.5"},
            )
        assert ok is False

    def test_slack_no_transport_drops(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            # room_session_manager exists but has no slack_transport attr.
            rsm = MagicMock(spec=[])  # no slack_transport
            mock_di.room_session_manager = rsm
            ok = publisher.publish(
                sender="Webby", text="hi",
                reply_to={"type": "slack", "channel_id": "C123"},
            )
        assert ok is False

    def test_sender_already_embedded_not_double_prefixed(self, publisher):
        slack_transport = MagicMock()
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(slack_transport=slack_transport)
            publisher.publish(
                sender="Webby",
                text="[Webby] On it",
                reply_to={"type": "slack", "channel_id": "C", "thread_ts": "1"},
            )
        body = slack_transport.send_reply.call_args.kwargs["body"]
        assert body == "[Webby] On it"  # no double-prefix


class TestTelegramRouting:

    def test_telegram_calls_telegram_transport_send_reply(self, publisher):
        telegram_transport = MagicMock()
        rsm = MagicMock(telegram_transport=telegram_transport)
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = rsm
            ok = publisher.publish(
                sender="Webby",
                text="Looking up the answer",
                reply_to={
                    "type": "telegram",
                    "chat_id": "123456",
                    "room_id": "telegram::123456",
                    "room_surface": "telegram",
                },
            )
        assert ok is True
        telegram_transport.send_reply.assert_called_once()
        kwargs = telegram_transport.send_reply.call_args.kwargs
        assert kwargs["chat_id"] == "123456"
        assert kwargs["body"] == "[Webby] Looking up the answer"

    def test_telegram_missing_chat_id_drops(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(telegram_transport=MagicMock())
            ok = publisher.publish(
                sender="Webby", text="hi",
                reply_to={"type": "telegram"},
            )
        assert ok is False

    def test_telegram_no_transport_drops(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(spec=[])  # no telegram_transport
            ok = publisher.publish(
                sender="Webby", text="hi",
                reply_to={"type": "telegram", "chat_id": "123"},
            )
        assert ok is False


class TestEmbedSenderOptOut:
    """embed_sender=False sends the raw text — the EmiEventRelay delegation
    path uses this because its messages are the assistant's own replies,
    not narrator/worker lines that need a [sender] tag."""

    def test_slack_embed_sender_false_sends_raw_text(self, publisher):
        slack_transport = MagicMock()
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(slack_transport=slack_transport)
            ok = publisher.publish(
                sender="assistant", text="On it",
                reply_to={"type": "slack", "channel_id": "C123", "thread_ts": "1.0"},
                embed_sender=False,
            )
        assert ok is True
        assert slack_transport.send_reply.call_args.kwargs["body"] == "On it"

    def test_telegram_embed_sender_false_sends_raw_text(self, publisher):
        telegram_transport = MagicMock()
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(telegram_transport=telegram_transport)
            ok = publisher.publish(
                sender="assistant", text="Done!",
                reply_to={"type": "telegram", "chat_id": "42"},
                embed_sender=False,
            )
        assert ok is True
        assert telegram_transport.send_reply.call_args.kwargs["body"] == "Done!"

    def test_sms_embed_sender_false_sends_raw_text(self, publisher):
        sms_transport = MagicMock()
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(sms_transport=sms_transport)
            ok = publisher.publish(
                sender="assistant", text="Reminder!",
                reply_to={"type": "twilio_sms", "to": "+1555", "from": "+1444"},
                embed_sender=False,
            )
        assert ok is True
        assert sms_transport.send_reply.call_args.kwargs["body"] == "Reminder!"


class TestUnknownSurface:

    def test_unknown_surface_returns_false(self, publisher):
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock()
            ok = publisher.publish(
                sender="X", text="hi",
                reply_to={"type": "carrier_pigeon", "where": "the sky"},
            )
        assert ok is False


# ── Failure isolation ────────────────────────────────────────────


class TestFailureIsolation:

    def test_transport_exception_caught(self, publisher):
        slack_transport = MagicMock()
        slack_transport.send_reply.side_effect = RuntimeError("network down")
        with patch("app.assistant.chat_outbound.outbound_chat_publisher.DI") as mock_di:
            mock_di.room_session_manager = MagicMock(slack_transport=slack_transport)
            # Should not raise.
            ok = publisher.publish(
                sender="X", text="hi",
                reply_to={"type": "slack", "channel_id": "C123", "thread_ts": ""},
            )
        assert ok is False
