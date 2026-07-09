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



def test_reminder_persists_durably_and_tickets_when_no_client(monkeypatch):
    """Delivery audit D3: a fired reminder always writes a unified_log row;
    with no live master_room socket it also rides the ticket channel."""
    import app.assistant.agent_classes.EmiReminderHandler as mod
    from app.services.socket_manager import RoomNotBound

    saved = []
    monkeypatch.setattr(
        "app.assistant.message_manager.save_to_unified_db.save_to_unified_db",
        lambda messages, source, **kw: saved.append((messages, source)),
    )
    tickets = []
    monkeypatch.setattr(
        "app.assistant.ticket_manager.ticket_service.propose_notice_ticket",
        lambda **kw: tickets.append(kw) or "t1",
    )

    class _NoClientSocketManager:
        @staticmethod
        def resolve_socket(_room):
            raise RoomNotBound(_room)

    from app.assistant.ServiceLocator import service_locator
    monkeypatch.setattr(service_locator.DI, "socket_manager", _NoClientSocketManager(), raising=False)

    agent = EmiReminderHandler.__new__(EmiReminderHandler)
    agent.name = "emi_reminder_handler"
    agent._persist_reminder_durably("Take out the trash at 8pm")

    assert len(saved) == 1
    messages, source = saved[0]
    assert source == "scheduler_reminder"
    assert "Take out the trash" in messages[0]["message"]

    assert len(tickets) == 1
    assert tickets[0]["ticket_type"] == "cron_reminder"
    assert tickets[0]["message"] == "Take out the trash at 8pm"


def test_reminder_no_ticket_when_client_live(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "app.assistant.message_manager.save_to_unified_db.save_to_unified_db",
        lambda messages, source, **kw: saved.append((messages, source)),
    )
    tickets = []
    monkeypatch.setattr(
        "app.assistant.ticket_manager.ticket_service.propose_notice_ticket",
        lambda **kw: tickets.append(kw) or "t1",
    )

    class _LiveSocketManager:
        @staticmethod
        def resolve_socket(_room):
            return "socket-abc"

    from app.assistant.ServiceLocator import service_locator
    monkeypatch.setattr(service_locator.DI, "socket_manager", _LiveSocketManager(), raising=False)

    agent = EmiReminderHandler.__new__(EmiReminderHandler)
    agent.name = "emi_reminder_handler"
    agent._persist_reminder_durably("Stretch break")

    assert len(saved) == 1  # unified_log row ALWAYS written
    assert tickets == []    # live client -> normal socket delivery, no ticket
