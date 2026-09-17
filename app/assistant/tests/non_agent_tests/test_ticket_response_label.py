"""A ticket response records what the user pressed and what they typed — nothing else.

2026-09-16. The service used to translate the action token into a canned sentence of our
own: `acknowledge` became "User has acknowledged this advice but has not committed to
action yet." On 09-15 that sentence was stored directly in front of the user's own words —
"the picture was already taken so this is all moot" — and every agent downstream read
both halves, the first of which contradicted the second.

The button's wording belongs to the surface that drew it (the same token reads "Acknowledge"
on the advice layout and "OK" on the notify layout, and those words are expected to change),
so it travels with the response. Absent a label, the action token is recorded — never prose.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.ticket_manager.ticket_service import TicketService


class _Manager:
    """Enough ticket manager to drive respond()."""

    def __init__(self):
        self.ticket = SimpleNamespace(ticket_id="t1", user_text=None, user_action=None,
                                      trigger_context={})
        self.saved = False
        self.transitions = []

    def get_ticket_by_id(self, ticket_id):
        return self.ticket if ticket_id == "t1" else None

    def save_ticket(self, ticket):
        self.saved = True
        return True

    def mark_accepted(self, ticket_id, user_text=""):
        self.transitions.append(("accepted", user_text))
        return True

    def mark_dismissed(self, ticket_id, user_text=""):
        self.transitions.append(("dismissed", user_text))
        return True

    def mark_expired(self, ticket_id, reason=""):
        self.transitions.append(("expired", reason))
        return True

    def mark_snoozed(self, ticket_id, snooze_minutes=30, user_text=""):
        self.transitions.append(("snoozed", user_text))
        return True


def _service(monkeypatch):
    published = []
    monkeypatch.setattr(TicketService, "_publish_ticket_responded",
                        staticmethod(lambda *a, **kw: published.append(a)))
    manager = _Manager()
    return TicketService(ticket_manager=manager), manager, published


def test_button_text_and_typed_words_both_survive(monkeypatch):
    service, manager, _ = _service(monkeypatch)
    service.respond("t1", "acknowledge", user_text="the picture was already taken.",
                    label="👍 Acknowledge")
    assert manager.ticket.user_text == "👍 Acknowledge — the picture was already taken."
    assert manager.ticket.user_action == "acknowledge"


def test_button_alone_is_the_whole_response(monkeypatch):
    service, manager, _ = _service(monkeypatch)
    service.respond("t1", "willdo", label="✓ Will Do")
    assert manager.ticket.user_text == "✓ Will Do"


def test_same_token_records_the_label_it_was_given(monkeypatch):
    """`acknowledge` is "OK" on the notify layout. Nothing server-side may override that."""
    service, manager, _ = _service(monkeypatch)
    service.respond("t1", "acknowledge", label="OK")
    assert manager.ticket.user_text == "OK"


def test_answer_actions_pass_the_words_through_verbatim(monkeypatch):
    """ask_user's contract: the typed text IS the answer and reaches the calling agent
    with no button name in front of it."""
    service, manager, _ = _service(monkeypatch)
    service.respond("t1", "answer", user_text="the student is a junior.", label="✓ Submit")
    assert manager.ticket.user_text == "the student is a junior."


def test_a_surface_without_buttons_records_the_token_not_prose(monkeypatch):
    service, manager, _ = _service(monkeypatch)
    service.respond("t1", "dismiss")
    assert manager.ticket.user_text == "dismiss"


def test_closing_is_recorded_and_published(monkeypatch):
    """Closing is a result too: a node waiting on this ticket is waiting on a tool call,
    and "the user closed it" ends that call instead of leaving it to time out."""
    service, manager, published = _service(monkeypatch)
    result = service.respond("t1", "close", label="Close without action")
    assert result.success
    assert manager.ticket.user_action == "close"
    assert manager.ticket.user_text == "Close without action"
    assert published, "a close must reach the listeners that land results"
