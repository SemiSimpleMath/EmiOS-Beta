"""EmiEventRelay transport delegation (delivery audit D1).

The relay used to carry its own slack/telegram/twilio_sms send branches —
a second, drifting copy of OutboundChatPublisher's dispatch (and the Slack
one bypassed the allow_real_slack_send gate). Now the relay keeps only the
socketio terminal and delegates every other surface to the publisher.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.emi_event_relay.emi_event_relay import EmiEventRelay
from app.assistant.ServiceLocator import service_locator


def _relay() -> EmiEventRelay:
    # __init__ spawns threads + registers event handlers; the dispatch
    # method under test needs none of that.
    return EmiEventRelay.__new__(EmiEventRelay)


def _payload(chat: str) -> SimpleNamespace:
    return SimpleNamespace(chat=chat, feed="", widget_data=None, sound=None)


def _message(sender: str = "assistant") -> SimpleNamespace:
    return SimpleNamespace(sender=sender, metadata={}, request_id=None, sub_data_type=[])


class _CapturingPublisher:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_transport_surfaces_delegate_to_publisher(monkeypatch):
    publisher = _CapturingPublisher()
    monkeypatch.setattr(
        service_locator.DI, "outbound_chat_publisher", publisher, raising=False,
    )
    relay = _relay()

    for reply_to in (
        {"type": "slack", "channel_id": "C1", "thread_ts": "1.0"},
        {"type": "telegram", "chat_id": "42"},
        {"type": "twilio_sms", "to": "+1555", "from": "+1444"},
    ):
        relay._emit_message(_message(), _payload("hello"), preferred_reply_to=reply_to)

    assert len(publisher.calls) == 3
    for call, expected_type in zip(publisher.calls, ("slack", "telegram", "twilio_sms")):
        assert call["reply_to"]["type"] == expected_type
        assert call["text"] == "hello"
        assert call["embed_sender"] is False  # the relay speaks as the assistant itself
        assert call["sender"] == "assistant"


def test_socketio_stays_inline_no_publisher(monkeypatch):
    """The relay IS the socket terminal — delegating socketio to the
    publisher would publish socket_emit right back to the relay (a loop)."""
    publisher = _CapturingPublisher()
    monkeypatch.setattr(
        service_locator.DI, "outbound_chat_publisher", publisher, raising=False,
    )
    relay = _relay()
    emitted = []
    relay._emit_via_socketio = lambda **kw: emitted.append(kw)

    relay._emit_message(
        _message(), _payload("hi"),
        preferred_reply_to={"type": "socketio", "room_id": "master_room"},
    )

    assert publisher.calls == []
    assert len(emitted) == 1
    assert emitted[0]["event"] == "user_message_data"
    assert emitted[0]["payload"]["chat"] == "hi"


def test_empty_chat_on_transport_surface_not_published(monkeypatch):
    publisher = _CapturingPublisher()
    monkeypatch.setattr(
        service_locator.DI, "outbound_chat_publisher", publisher, raising=False,
    )
    relay = _relay()

    relay._emit_message(
        _message(), _payload("   "),
        preferred_reply_to={"type": "telegram", "chat_id": "42"},
    )

    assert publisher.calls == []


def test_handler_resolves_reply_to_once_for_tts_and_queue():
    """Delivery audit D4: socket_emit_handler resolves the destination ONCE
    and hands the resolved dict to both the TTS job and the queued emit.
    Each used to resolve independently — an unpinned message logged the
    defaulted-to-master_room WARN twice and could race the reply_router
    TTL to two different answers."""
    import queue as queue_mod

    relay = _relay()
    relay.message_queue = queue_mod.Queue()
    submitted = []
    relay.tts_executor = SimpleNamespace(
        submit=lambda fn, *args: submitted.append(args),
    )
    resolve_calls = []
    real_resolve = relay._resolve_reply_to

    def _counting_resolve(message, preferred_reply_to=None):
        resolve_calls.append(preferred_reply_to)
        return real_resolve(message, preferred_reply_to=preferred_reply_to)

    relay._resolve_reply_to = _counting_resolve

    msg = SimpleNamespace(sender="assistant", metadata={}, request_id=None, sub_data_type=[])
    payload = SimpleNamespace(
        chat="hi", feed="", widget_data=None, sound=None, tts=True, tts_text="hi",
    )

    relay.socket_emit_handler(SimpleNamespace(user_message_data=payload, **msg.__dict__))

    assert len(resolve_calls) == 1              # one resolution per message
    resolved = {"type": "socketio", "room_id": "master_room"}
    assert submitted[0][2] == resolved          # TTS job got the resolved dict
    _, _, queued_reply_to = relay.message_queue.get_nowait()
    assert queued_reply_to == resolved          # queued emit got the same dict
