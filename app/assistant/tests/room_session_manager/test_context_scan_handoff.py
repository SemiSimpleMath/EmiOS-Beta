"""Context scanning uses prepared history and never waits for its model on chat delivery."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.assistant.room_session_manager.room_session_manager import RoomSessionManager
from app.assistant.context_engine import pipeline, chat_scan


def test_room_handoff_reuses_prepared_history(monkeypatch):
    from app.assistant.utils import subsystem_flags
    from app.assistant.kg_core import user_identity
    monkeypatch.setattr(subsystem_flags, "is_subsystem_enabled", lambda name: True)
    monkeypatch.setattr(user_identity, "get_primary_user_name", lambda: "Alex")
    trigger = Mock(return_value=True)
    monkeypatch.setattr(pipeline, "maybe_trigger_pipeline", trigger)
    manager = RoomSessionManager.__new__(RoomSessionManager)
    manager.history_builder = Mock()
    history = [{"sender": "Alex", "role": "user", "content": "My brother Morgan is visiting.", "room_id": "master_room"}]
    envelope = SimpleNamespace(room_id="master_room", content="He arrives tomorrow.", request_id="test")
    manager._maybe_trigger_context_engine(envelope=envelope, request_data={"seeded_chat_messages": history})
    args = trigger.call_args.kwargs
    assert json.loads(args["recent_chat_context"])[0]["content"] == history[0]["content"]
    assert args["user_message"] == "He arrives tomorrow."
    manager.history_builder.build_messages.assert_not_called()


def test_scanner_receives_history_and_renders_it_in_jinja(monkeypatch):
    from app.assistant.ServiceLocator import service_locator
    captured = []
    def handle(message):
        captured.append(message)
        return SimpleNamespace(data={"should_activate": True, "seeds": ["Alex", "Morgan"], "reason": "Named in prior turn"})
    monkeypatch.setattr(service_locator, "DI", SimpleNamespace(agent_factory=SimpleNamespace(create_agent=lambda name: SimpleNamespace(action_handler=handle))))
    result = chat_scan.scan_for_activation("He arrives tomorrow.", "Alex", recent_chat_context="Alex: My brother Morgan is visiting.", owner_id="master_room")
    root = Path(__file__).resolve().parents[2] / "agents" / "context_engine" / "chat_scan" / "prompts"
    env = Environment(loader=FileSystemLoader(str(root)), undefined=StrictUndefined)
    rendered = env.get_template("user.j2").render(task=captured[0].task, **captured[0].agent_input)
    assert "Morgan is visiting" in rendered
    assert "He arrives tomorrow" in rendered
    assert result.seeds == ["Alex", "Morgan"]


def test_pipeline_trigger_returns_while_scan_is_still_running(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    received = {}
    def run(**kwargs):
        received.update(kwargs)
        started.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
    monkeypatch.setattr(pipeline, "run_context_activation_pipeline", run)
    try:
        assert pipeline.maybe_trigger_pipeline(user_message="He arrives tomorrow.", primary_user="Alex", owner_id="test_async_context", recent_chat_context="Morgan is visiting")
        assert started.wait(2)
        assert not finished.is_set()
        assert received["recent_chat_context"] == "Morgan is visiting"
    finally:
        release.set()
        assert finished.wait(2)
