"""One event-registration convention (EventHub audit E3, 2026-07-09).

Agent event handlers registered through two paths: AgentFactory
(un-namespaced `register_event(topic, handler)` — the live path) and
AgentLoader.register_agent_events (namespaced `name:topic` — proven dead
in live boot logs: only emi_reminder_handler has an events: list and it
loads via the factory). The dead namespaced path is removed; this pins
that AgentLoader no longer registers events and the factory path stays
un-namespaced.
"""
from __future__ import annotations


def test_agent_loader_has_no_event_registration_path():
    from app.assistant.agent_registry import agent_loader

    # The dead namespaced convention is gone entirely.
    assert not hasattr(agent_loader.AgentLoader, "register_agent_events")
    # And its source no longer references the hub (DI import removed with it).
    import inspect
    src = inspect.getsource(agent_loader)
    assert "register_agent_events" not in src
    assert "{agent_instance.name}:{event}" not in src


def test_factory_registers_un_namespaced():
    """The one live convention: AgentFactory registers the bare config
    topic, so a publisher emitting event_topic='X' reaches the handler."""
    import inspect

    from app.assistant.agent_registry import agent_factory

    src = inspect.getsource(agent_factory)
    # register_event is called with the bare `event`, not a namespaced key.
    assert "register_event(event, _serialized_handler)" in src
    assert "register_event(f\"{" not in src  # no namespaced form
