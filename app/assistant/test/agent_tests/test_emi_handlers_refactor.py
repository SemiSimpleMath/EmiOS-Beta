import pytest

from app.assistant.agent_classes.EmiReminderHandler import EmiReminderHandler
from app.assistant.agent_classes.EmiResultHandler import EmiResultHandler
from app.assistant.utils.pydantic_classes import Message


class _MsgStore:
    def __init__(self):
        self.items = []

    def add_msg(self, msg):
        self.items.append(msg)


class _EventHub:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class _LocalBlackboard:
    def __init__(self):
        self.items = []

    def add_msg(self, msg):
        self.items.append(msg)


def test_emi_result_handler_process_llm_result_handles_non_string_fields(monkeypatch):
    from app.assistant.agent_classes import EmiResultHandler as mod

    global_store = _MsgStore()
    event_hub = _EventHub()
    monkeypatch.setattr(mod.DI, "global_blackboard", global_store)
    monkeypatch.setattr(mod.DI, "event_hub", event_hub)

    agent = EmiResultHandler.__new__(EmiResultHandler)
    agent.name = "emi_result_handler"
    agent._active_request_id = "req-1"
    agent._active_reply_to = {"type": "socketio", "room_id": "abc"}

    response = {
        "task": "Task header",
        "answer": None,
        "interesting_info": {"k": "v"},
        "methods": ["a", "b"],
        "sources": None,
        "meta_data": {"score": 1},
        "feed": {"feed": "ok"},
    }

    out = EmiResultHandler.process_llm_result(agent, response)
    assert out is None
    assert len(global_store.items) == 1
    assert len(event_hub.published) == 1
    emitted = event_hub.published[0]
    assert emitted.request_id == "req-1"
    assert emitted.metadata["reply_to"]["room"] == "abc"


def test_emi_result_handler_action_handler_requires_tool_result():
    agent = EmiResultHandler.__new__(EmiResultHandler)
    agent.name = "emi_result_handler"
    agent._set_agent_busy = lambda: None
    agent._set_agent_idle = lambda: None
    agent._update_blackboard_state = lambda _msg: None
    agent.blackboard = _LocalBlackboard()

    with pytest.raises(ValueError, match="tool_result is required"):
        EmiResultHandler.action_handler(agent, Message(event_topic="emi_result_request", tool_result=None))


def test_emi_reminder_handler_action_handler_validates_scheduler_payload(monkeypatch):
    from app.assistant.agent_classes import EmiReminderHandler as mod

    class _Settings:
        @staticmethod
        def is_quiet_mode_active(_feature):
            return False

    monkeypatch.setattr("app.assistant.user_settings_manager.user_settings.can_run_feature", lambda _feature: True)
    monkeypatch.setattr("app.assistant.user_settings_manager.user_settings.get_settings_manager", lambda: _Settings())

    agent = EmiReminderHandler.__new__(EmiReminderHandler)
    agent.name = "emi_reminder_handler"
    agent._set_agent_busy = lambda: None
    agent._set_agent_idle = lambda: None
    agent._update_blackboard_state = lambda _msg: None
    agent.blackboard = _LocalBlackboard()

    with pytest.raises(ValueError, match="event_payload"):
        EmiReminderHandler.action_handler(
            agent,
            Message(event_topic="scheduler_event_interval", data={"wrong": "shape"}),
        )

