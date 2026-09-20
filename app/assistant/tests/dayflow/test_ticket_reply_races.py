"""A durable ticket reply cannot be lost between publication and listener registration."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from app.assistant.lib.tools.create_dayflow_ticket import create_dayflow_ticket as ticket_tool
from app.assistant.ServiceLocator.service_locator import DI


def answered_ticket():
    return SimpleNamespace(ticket_id="ticket", state="accepted", title="Review", user_text="Already approved",
        user_action="acknowledge", valid_until=datetime.now(timezone.utc) + timedelta(hours=1))


def test_reply_before_listener_registration_is_returned_immediately(monkeypatch):
    tm = SimpleNamespace(get_ticket_by_id=lambda tid: answered_ticket(), mark_expired=Mock())
    monkeypatch.setattr(ticket_tool, "get_ticket_manager", lambda: tm)
    monkeypatch.setattr(DI, "event_hub", SimpleNamespace(register_event=Mock(), unregister_event=Mock(), publish=Mock()))
    result = ticket_tool.CreateDayflowTicketTool._wait_for_ticket_response("ticket", "Review", 0)
    assert result.data["action"] == "acknowledge"
    assert "Already approved" in result.content
    tm.mark_expired.assert_not_called()


def test_reply_winning_the_expiry_race_is_returned_as_a_reply(monkeypatch):
    ticket = answered_ticket()
    ticket.state = "proposed"
    def expire(*args, **kwargs):
        ticket.state = "accepted"
        return False  # The reply committed first; expiry was refused.
    tm = SimpleNamespace(get_ticket_by_id=lambda tid: ticket, mark_expired=expire)
    monkeypatch.setattr(ticket_tool, "get_ticket_manager", lambda: tm)
    monkeypatch.setattr(DI, "event_hub", SimpleNamespace(register_event=Mock(), unregister_event=Mock(), publish=Mock()))
    result = ticket_tool.CreateDayflowTicketTool._wait_for_ticket_response("ticket", "Review", 0)
    assert result.data["action"] == "acknowledge"


def test_ticket_execution_preserves_context_binding_and_action_ledger(monkeypatch):
    from work_objects.store import WorkStore
    from app.assistant.dayflow_orchestrator import work_store
    from app.assistant.utils.pydantic_classes import ToolResult
    store = WorkStore(":memory:")
    wo = store.apply("create_work_object", {"title": "Ask the user"})
    store.apply("add_node", {"work_id": wo.id, "id": "ask", "type": "subtask", "parent_id": wo.goal_node_id})
    store.apply("set_status", {"work_id": wo.id, "node_id": "ask", "status": "dispatched"})
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: store)
    ticket = SimpleNamespace(ticket_id="ticket", message="Question", to_dict=lambda: {"ticket_id": "ticket", "title": "Review", "message": "Question"})
    manager = SimpleNamespace(get_tickets_pending_or_proposed=lambda: [], create_ticket=Mock(return_value=ticket), mark_proposed=lambda tid: True)
    monkeypatch.setattr(ticket_tool, "get_ticket_manager", lambda: manager)
    hub = SimpleNamespace(publish=Mock())
    monkeypatch.setattr(DI, "event_hub", hub)
    formatter = Mock(return_value={"title": "Review", "message": "Question", "ticket_kind": "notify", "suggestion_type": "task_update"})
    monkeypatch.setattr(ticket_tool.CreateDayflowTicketTool, "_format_brief", staticmethod(formatter))
    monkeypatch.setattr(ticket_tool.CreateDayflowTicketTool, "_wait_for_ticket_response", staticmethod(lambda *args: ToolResult(result_type="ticket_response", content="Approved", data={})))
    ref = f"{wo.id}::ask"
    result = ticket_tool.CreateDayflowTicketTool().execute(SimpleNamespace(tool_data={"arguments": {
        "ticket_brief": "Review the plan", "trigger_context": {"work_node": ref, "dispatch_epoch": 1},
        "speak_tts": False, "valid_hours": 1, "wait_timeout_seconds": 3600}}))
    assert result.result_type == "ticket_response"
    formatter.assert_called_once_with("Review the plan", work_node_ref=ref)
    saved = store.load(wo.id)
    assert saved.nodes["ask"].wake_kind == "user_reply"
    assert saved.nodes["ask"].wake_ref == "ticket"
    assert saved.nodes["ask"].payload["ticket_epoch"] == 1
    assert len(saved.actions) == 1
    assert saved.actions[0].payload["dispatch_epoch"] == 1
    assert manager.create_ticket.call_args.kwargs["trigger_context"]["dispatch_epoch"] == 1
    store.close()


def test_reply_cannot_restore_a_ticket_expired_after_the_initial_read(tmp_path, monkeypatch):
    import importlib
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.assistant.ticket_manager.ticket import Ticket
    from app.assistant.ticket_manager.ticket_service import TicketService
    module = importlib.import_module("app.assistant.ticket_manager.ticket_manager")
    engine = create_engine("sqlite:///" + str(tmp_path / "ticket-race.db"))
    Ticket.__table__.create(engine)
    monkeypatch.setattr(module, "get_session", sessionmaker(bind=engine))
    manager = module.TicketManager()
    ticket = manager.create_ticket(ticket_type="dayflow_notify", suggestion_type="test", title="Question", message="Test")
    manager.mark_proposed(ticket.ticket_id)
    predicate = manager.can_transition
    def expire_after_read(state, target):
        manager.mark_expired(ticket.ticket_id)
        return predicate(state, target)
    monkeypatch.setattr(manager, "can_transition", expire_after_read)
    publish = Mock()
    monkeypatch.setattr(TicketService, "_publish_ticket_responded", publish)
    response = TicketService(manager).respond(ticket.ticket_id, "acknowledge", user_text="Late answer")
    current = manager.get_ticket_by_id(ticket.ticket_id)
    assert not response.success
    assert response.not_answerable
    assert current.state == "expired"
    assert not current.user_text
    publish.assert_not_called()
    engine.dispose()
