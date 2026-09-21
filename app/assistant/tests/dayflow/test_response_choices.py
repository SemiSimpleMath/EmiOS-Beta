import pytest
from app.assistant.ticket_manager.response_choices import validate_choices


def choice(label="OK", meaning="acknowledge"):
    return {"label": label, "meaning": meaning, "scope": "Receipt of the lunch reminder"}


def test_choices_keep_receipt_separate_from_action():
    assert validate_choices([choice()]) == [{"id": "choice_1", **choice()}]
    assert validate_choices([choice("Handle myself", "handle_myself")])[0]["meaning"] == "handle_myself"


@pytest.mark.parametrize("values", [[], [choice()] * 4, [choice(), choice()],
    [choice("Would you do this")], [choice("Yes?")], [choice("x" * 25)],
    [choice("OK", "execute_tool")], [choice("Yes\nNo")]])
def test_invalid_choices_rejected(values):
    with pytest.raises(ValueError):
        validate_choices(values)


@pytest.fixture
def manager(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.assistant.ticket_manager import ticket_manager as module
    from app.assistant.ticket_manager.ticket import Ticket
    engine = create_engine("sqlite:///" + str(tmp_path / "choices.db"))
    Ticket.__table__.create(engine)
    monkeypatch.setattr(module, "get_session", sessionmaker(bind=engine, expire_on_commit=False))
    yield module.TicketManager()
    engine.dispose()


def contextual_ticket(manager):
    ticket = manager.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type="test",
        title="Lunch", message="It is lunchtime", action_type="none",
        trigger_context={"response_choices": validate_choices([choice()])})
    manager.mark_proposed(ticket.ticket_id)
    return ticket.ticket_id


def test_atomic_receipt_duplicate_and_distinct_followup(manager):
    from app.assistant.ticket_manager.contextual_response import record_response
    tid = contextual_ticket(manager)
    first = record_response(manager, tid, "choice_1", "")
    assert first["action"] == "acknowledge"
    assert record_response(manager, tid, "choice_1", "")["duplicate"]
    assert record_response(manager, tid, "choice_1", "I am busy, not going now.")["action"] == "answer"
    saved = manager.get_ticket_by_id(tid)
    assert saved.user_text == "I am busy, not going now."
    assert saved.user_response_parsed["decision"] == "response"
    assert len(saved.user_response_parsed["response_history"]) == 2
    assert saved.user_response_parsed["response_history"][0]["meaning"] == "acknowledge"
    assert saved.user_response_parsed["followup"] is True


def test_forged_choice_and_expiry_do_not_write(manager):
    from app.assistant.ticket_manager.contextual_response import record_response
    tid = contextual_ticket(manager)
    with pytest.raises(ValueError):
        record_response(manager, tid, "not-a-choice", "yes")
    manager.mark_expired(tid)
    assert record_response(manager, tid, "choice_1", "yes") is None
    assert not manager.get_ticket_by_id(tid).user_text
