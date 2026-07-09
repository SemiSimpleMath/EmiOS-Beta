import threading

import pytest

from app.assistant.agent_classes.EmiReminderHandler import EmiReminderHandler
from app.assistant.utils.pydantic_classes import Message


class _LocalBlackboard:
    def __init__(self):
        self.items = []

    def add_msg(self, msg):
        self.items.append(msg)


# EmiResultHandler (and its two tests here) was DELETED 2026-07-08: it had
# no live invoker — nothing published "emi_result_request" and nothing
# called the DI.emi_result_handler service boot used to register. Its
# chat-relay job lives in the one-shot/ticket/router paths today.


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
    agent._handle_lock = threading.Lock()
    agent._set_agent_busy = lambda: None
    agent._set_agent_idle = lambda: None
    agent._update_blackboard_state = lambda _msg: None
    agent.blackboard = _LocalBlackboard()

    with pytest.raises(ValueError, match="event_payload"):
        EmiReminderHandler.action_handler(
            agent,
            Message(event_topic="scheduler_event_interval", data={"wrong": "shape"}),
        )

