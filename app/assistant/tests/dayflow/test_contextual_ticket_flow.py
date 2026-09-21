from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.assistant.tests.dayflow.test_response_choices import manager, choice, contextual_ticket
from app.assistant.ticket_manager.ticket_service import TicketService
from app.assistant.ticket_manager.ticket import TicketState
from app.assistant.lib.tools.create_dayflow_ticket import create_dayflow_ticket as tool
from app.assistant.ServiceLocator.service_locator import DI


def test_service_uses_stored_label_and_publishes_once(manager, monkeypatch):
    tid = contextual_ticket(manager)
    published = Mock()
    monkeypatch.setattr(TicketService, "_publish_ticket_responded", published)
    svc = TicketService(manager)
    assert svc.respond(tid, "choice", choice_id="choice_1", label="Approve everything").success
    assert svc.respond(tid, "choice", choice_id="choice_1").success
    assert published.call_count == 1
    saved = manager.get_ticket_by_id(tid)
    assert saved.user_text == "OK"
    result = tool.CreateDayflowTicketTool.result_for_ticket(saved)
    assert "Button meaning: acknowledge" in result.content
    assert "not consent" in result.content
    assert svc.respond(tid, "choice", choice_id="choice_1", user_text="I am busy.").success
    assert published.call_count == 2
    saved = manager.get_ticket_by_id(tid)
    assert saved.user_text == "I am busy."
    assert "I am busy." in tool.CreateDayflowTicketTool.result_for_ticket(saved).content
    assert not svc.respond(tid, "choice", choice_id="forged").success


def test_legacy_same_state_cannot_report_unsaved_text(manager):
    tid = contextual_ticket(manager)
    assert manager.transition_state(tid, TicketState.ACCEPTED, user_text="First")
    assert not manager.transition_state(tid, TicketState.ACCEPTED, user_text="Different")
    assert manager.get_ticket_by_id(tid).user_text == "First"
    assert manager.transition_state(tid, TicketState.ACCEPTED, user_text="First")


def test_standard_composer_to_persisted_choices_to_written_reply(manager, monkeypatch):
    from app.assistant.agents.ticket_builder.composer.agent_form import AgentForm
    from app.assistant.scope import loader
    form = AgentForm(reasoning="Informational", ticket_kind="notify", suggestion_type="routine",
        title="Lunch", message="It is lunchtime", response_choices=[choice()])
    composed = SimpleNamespace(sender="ticket_builder::composer", data=form.model_dump())
    mgr = SimpleNamespace(blackboard=SimpleNamespace(get_messages=lambda: [composed]))
    monkeypatch.setattr(DI, "multi_agent_manager_factory", SimpleNamespace(create_manager=lambda name: mgr))
    monkeypatch.setattr(DI, "manager_invoker", SimpleNamespace(invoke=Mock()))
    monkeypatch.setattr(loader, "load_scope_for_source", lambda **kw: None)
    monkeypatch.setattr(tool, "get_ticket_manager", lambda: manager)
    monkeypatch.setattr(DI, "event_hub", SimpleNamespace(publish=Mock()))
    result = tool.CreateDayflowTicketTool().execute(SimpleNamespace(tool_data={"arguments": {
        "ticket_brief": "Give a lunch reminder", "wait": False, "speak_tts": False}}))
    assert result.result_type != "error", result.content
    tickets = manager.get_tickets_pending_or_proposed()
    ticket, = tickets
    assert ticket.to_dict()["response_choices"][0]["label"] == "OK"
    svc = TicketService(manager)
    assert svc.respond(ticket.ticket_id, "answer", user_text="Already ate, thanks.").success
    saved = manager.get_ticket_by_id(ticket.ticket_id)
    assert saved.user_response_parsed["meaning"] == "answer"
    result = tool.CreateDayflowTicketTool.result_for_ticket(saved)
    assert "Already ate, thanks." in result.content
    from app.assistant.pipelines.dayflow.utils.context_sources import _format_ticket_for_context
    view = _format_ticket_for_context(saved)
    assert view["response_details"]["typed_text"] == "Already ate, thanks."


def test_contextual_choices_cannot_be_used_on_tool_approval(manager, monkeypatch):
    from app.assistant.ticket_manager.response_choices import validate_choices
    t=manager.create_ticket(ticket_type="tool_approval", suggestion_type="test", title="Permission", message="Approve?",
        trigger_context={"response_choices": validate_choices([choice()])})
    manager.mark_proposed(t.ticket_id)
    result=TicketService(manager).respond(t.ticket_id,"choice",choice_id="choice_1")
    assert not result.success
    assert manager.get_ticket_by_id(t.ticket_id).state == "proposed"
