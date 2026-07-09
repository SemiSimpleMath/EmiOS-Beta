"""bedroom_emergency_detected gets a consumer (EventHub audit E1, 2026-07-09).

The sleep post-handler published the emergency to zero subscribers — the
alert reached only a disk sidecar. EmergencyNotifier is the missing
consumer: it subscribes at construction and surfaces the emergency as a
durable high-priority owner ticket.
"""
from __future__ import annotations

import app.assistant.ring_analysis.emergency_notifier as en
from app.assistant.utils.pydantic_classes import Message


class _FakeHub:
    def __init__(self):
        self.registered = {}

    def register_event(self, topic, handler):
        self.registered[topic] = handler


def _install(monkeypatch):
    hub = _FakeHub()
    monkeypatch.setattr(en.DI, "event_hub", hub, raising=False)
    tickets = []
    monkeypatch.setattr(
        "app.assistant.ticket_manager.ticket_service.propose_notice_ticket",
        lambda **kw: tickets.append(kw) or "emergency-ticket-1",
    )
    return hub, tickets


def test_subscribes_at_construction(monkeypatch):
    hub, _ = _install(monkeypatch)
    en.EmergencyNotifier()
    assert "bedroom_emergency_detected" in hub.registered


def test_emergency_surfaces_a_high_priority_ticket(monkeypatch):
    hub, tickets = _install(monkeypatch)
    en.EmergencyNotifier()

    hub.registered["bedroom_emergency_detected"](Message(
        data_type="event",
        sender="bedroom_emergency_alarm",
        event_topic="bedroom_emergency_detected",
        content="Person on the floor, not moving",
        data={
            "description": "Person on the floor, not moving",
            "importance_reason": "possible fall",
            "camera_name": "Bedroom",
            "detected_at_local": "2026-07-09 03:14",
            "camera_id": "cam_bedroom",
            "frame": "/data/frame.jpg",
            "sidecar": "/data/frame.EMERGENCY.txt",
        },
    ))

    assert len(tickets) == 1
    t = tickets[0]
    assert t["suggestion_type"] == "bedroom_emergency"
    assert t["valid_hours"] == 168                       # a week — must not silently expire
    assert "Bedroom" in t["message"]
    assert "possible fall" in t["message"]
    assert t["trigger_context"]["frame"] == "/data/frame.jpg"


def test_ticket_channel_down_is_survivable(monkeypatch):
    hub, _ = _install(monkeypatch)
    monkeypatch.setattr(
        "app.assistant.ticket_manager.ticket_service.propose_notice_ticket",
        lambda **kw: None,   # infra unavailable
    )
    en.EmergencyNotifier()
    # Must not raise — the upstream log + sidecar remain the record of the event.
    hub.registered["bedroom_emergency_detected"](Message(
        event_topic="bedroom_emergency_detected", content="x", data={"description": "x"},
    ))
