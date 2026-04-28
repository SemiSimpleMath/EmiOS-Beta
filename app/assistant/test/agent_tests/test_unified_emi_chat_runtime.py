import os
import tempfile
from datetime import datetime, timezone

from app.assistant.agent_runtime.services.context_injector import ContextInjector
from app.assistant.agent_runtime.services.chat_request_normalizer import ChatRequestNormalizer
from app.assistant.agent_runtime.services.chat_response_builder import ChatResponseBuilder
from app.assistant.agent_runtime.services.chat_publisher import ChatPublisher
from app.assistant.agent_runtime.services.llm_client import LLMClient
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData


class _Message:
    def __init__(self, *, event_topic=None, agent_input=None, metadata=None):
        self.event_topic = event_topic
        self.agent_input = agent_input
        self.metadata = metadata


def test_chat_request_normalizer_sets_speaking_mode_from_tts_flag():
    normalizer = ChatRequestNormalizer()
    msg = _Message(metadata={"reply_to": {"type": "socketio"}, "tts_requested": True})
    meta = normalizer.normalize_interaction_metadata(msg)
    assert meta["interaction_mode"] == "speaking"
    assert meta["tts_requested"] is True


def test_chat_request_normalizer_builds_slash_payload():
    normalizer = ChatRequestNormalizer()
    msg = _Message(agent_input="/music play some jazz")
    payload = normalizer.build_user_history_payload(msg)
    assert payload["content"] == "play some jazz"
    assert payload["sub_data_type"] == ["slash_command", "music"]
    assert payload["metadata"]["command_name"] == "music"


def test_chat_response_builder_sets_tts():
    builder = ChatResponseBuilder()
    bb_msg, chat_msg = builder.build_user_response_messages(
        sender="emi_agent",
        chat_text="hello there",
        tts_requested=True,
    )
    assert bb_msg.content == "hello there"
    assert chat_msg.user_message_data.chat == "hello there"
    assert chat_msg.user_message_data.tts is True
    assert chat_msg.user_message_data.tts_text == "hello there"


def test_llm_client_multimodal_routes_openai_and_cleans_up(monkeypatch):
    class _Blackboard:
        @staticmethod
        def get_state_value(_key):
            return None

    class _Agent:
        name = "emi_agent"
        llm_params = {"llm_provider": "gemini", "engine": "gemini-3-flash-preview", "temperature": 0.8}
        blackboard = _Blackboard()
        config = {}

    class _Iface:
        @staticmethod
        def structured_output(*_args, **_kwargs):
            return {"ok": True}

    calls = []

    def _fake_get_llm_interface(**kwargs):
        calls.append(kwargs)
        return _Iface()

    monkeypatch.setattr("app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface", _fake_get_llm_interface)

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)

    client = LLMClient()
    result = client.call_structured_output(
        agent=_Agent(),
        messages=[{"role": "user", "content": [{"type": "image_path", "path": path}]}],
        response_format=None,
        use_json=False,
    )

    assert result == {"ok": True}
    assert calls and calls[0].get("llm_provider") == "openai"
    assert not os.path.exists(path)


def test_chat_publisher_attaches_routing_metadata(monkeypatch):
    class _EventHub:
        def __init__(self):
            self.sent = []

        def publish(self, msg):
            self.sent.append(msg)

    class _Agent:
        _active_request_id = "request-123"
        _active_reply_to = {"type": "socketio", "room_id": "room-1", "tts_requested": True}

    event_hub = _EventHub()
    monkeypatch.setattr("app.assistant.agent_runtime.services.chat_publisher.DI.event_hub", event_hub)

    msg = UserMessage(
        data_type="user_msg",
        sender="emi_agent",
        role="assistant",
        user_message_data=UserMessageData(chat="hello"),
    )
    ChatPublisher().publish_chat_to_user(agent=_Agent(), message=msg)

    assert msg.event_topic == "socket_emit"
    assert msg.request_id == "request-123"
    assert msg.metadata["reply_to"]["room_id"] == "room-1"
    assert msg.metadata["tts_requested"] is True
    assert len(event_hub.sent) == 1


def test_context_injector_history_includes_recent_chat_and_excludes_room_and_summarized(monkeypatch):
    t0 = datetime(2026, 2, 16, 16, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 16, 16, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 16, 16, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 2, 16, 16, 3, tzinfo=timezone.utc)

    kept_1 = Message(
        data_type="user_msg",
        sender="User",
        content="orange",
        is_chat=True,
        timestamp=t0,
    )
    kept_2 = Message(
        data_type="user_msg",
        sender="Emi",
        content="Orange? Like the fruit?",
        is_chat=True,
        timestamp=t1,
    )
    room_chat = Message(
        data_type="user_msg",
        sender="User",
        content="this is room traffic",
        is_chat=True,
        room_id="justin",
        timestamp=t2,
    )
    summarized = Message(
        data_type="user_msg",
        sender="User",
        content="old summarized item",
        is_chat=True,
        metadata={"summarized": True},
        timestamp=t3,
    )

    class _GlobalBB:
        @staticmethod
        def get_messages():
            return [kept_1, kept_2, room_chat, summarized]

    class _LocalBB:
        @staticmethod
        def get_state_value(_key, default=None):
            return default

    class _Agent:
        name = "emi_agent"
        blackboard = _LocalBB()

    monkeypatch.setattr("app.assistant.agent_runtime.services.context_injector.DI.global_blackboard", _GlobalBB())

    context = ContextInjector().generate_injections_block(_Agent(), ["history"])
    history = context.get("history", "")

    assert "orange" in history
    assert "Orange? Like the fruit?" in history
    assert "this is room traffic" not in history
    assert "old summarized item" not in history

