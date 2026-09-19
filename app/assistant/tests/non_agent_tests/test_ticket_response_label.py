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

from app.assistant.ticket_manager.ticket import TicketState
from app.assistant.ticket_manager.ticket_manager import TicketManager
from app.assistant.ticket_manager.ticket_service import TicketService


class _Manager:
    """Enough ticket manager to drive respond()."""

    def __init__(self, state="proposed"):
        self.ticket = SimpleNamespace(ticket_id="t1", user_text=None, user_action=None,
                                      trigger_context={}, state=state)
        self.saved = False
        self.transitions = []

    # Delegates to the REAL predicate rather than restating the table: a fake that
    # answered this itself could drift from the state machine it stands in for, and the
    # bug this guards was precisely a caller not consulting the table at all.
    can_transition = staticmethod(TicketManager.can_transition)

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


def _service(monkeypatch, state="proposed"):
    published = []
    monkeypatch.setattr(TicketService, "_publish_ticket_responded",
                        staticmethod(lambda *a, **kw: published.append(a)))
    manager = _Manager(state=state)
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


# --------------------------------------------------------------------------- #
# A reply to a ticket that can no longer accept one (2026-09-18)
#
# create_dayflow_ticket marks its ticket EXPIRED the moment its wait times out, and
# _ALLOWED_TRANSITIONS gives `expired` no outgoing edges. respond() used to stamp the
# user's words onto the row FIRST and attempt the transition after, so a reply racing that
# timeout was persisted onto an expired ticket where result_for_ticket never looks, the
# transition was refused with a warning, and the API returned HTTP 200 with success=False
# and no error. The answer was in the database and unreachable, and nobody was told.
# --------------------------------------------------------------------------- #

def test_a_reply_to_an_expired_ticket_is_refused_before_anything_is_written(monkeypatch):
    service, manager, published = _service(monkeypatch, state="expired")
    result = service.respond("t1", "acknowledge", user_text="yes please", label="👍 OK")

    assert result.success is False
    assert result.not_answerable is True, "the surface must be able to tell this apart typed"
    assert result.error and "expired" in result.error

    # The defect: the answer must NOT be persisted onto a ticket that refuses it.
    assert manager.ticket.user_text is None
    assert manager.ticket.user_action is None
    assert manager.saved is False
    assert manager.transitions == []
    assert published == [], "nothing may be published for a response that did not happen"


def test_every_terminal_state_refuses_a_reply(monkeypatch):
    for state in ("expired", "dismissed", "completed", "failed"):
        service, manager, _ = _service(monkeypatch, state=state)
        result = service.respond("t1", "done", label="Done")
        assert result.success is False, f"{state} must refuse"
        assert result.not_answerable is True, f"{state} must be typed as unanswerable"
        assert manager.ticket.user_text is None, f"{state} must not store the answer"


def test_a_live_ticket_still_accepts_its_reply(monkeypatch):
    """The guard must not shut the door on the normal case.

    `proposed` is the ONLY state that both reaches the user and accepts an answer. Every
    one of the eleven create_ticket call sites pairs with mark_proposed, which is what puts
    a displayed ticket there.
    """
    service, manager, published = _service(monkeypatch, state="proposed")
    result = service.respond("t1", "done", label="Done")
    assert result.success is True
    assert result.not_answerable is False
    assert manager.ticket.user_text == "Done"
    assert published


def test_only_proposed_can_be_accepted(monkeypatch):
    """The state machine's shape, stated once so a reader need not reconstruct it.

    ACCEPTED is reachable from `proposed` alone. PENDING and SNOOZED both lead only to
    `proposed` or `expired`, so neither can take a reply directly.
    """
    assert TicketManager.can_transition("proposed", TicketState.ACCEPTED) is True
    for state in ("pending", "snoozed", "expired", "dismissed", "completed", "failed"):
        assert TicketManager.can_transition(state, TicketState.ACCEPTED) is False, state
    # Both non-terminal ones do reach the displayable state.
    assert TicketManager.can_transition("pending", TicketState.PROPOSED) is True
    assert TicketManager.can_transition("snoozed", TicketState.PROPOSED) is True


def test_a_pending_ticket_is_displayable_but_unanswerable(monkeypatch):
    """A latent trap, pinned rather than fixed.

    get_tickets_pending_or_proposed lists PENDING alongside PROPOSED, so a ticket left in
    PENDING is SHOWN to the user — and PENDING cannot reach ACCEPTED, so answering it
    fails. Unreachable today only by convention: create_ticket's docstring leaves
    mark_proposed to "the caller's responsibility", and all eleven callers happen to
    oblige. SNOOZED is not affected because that listing excludes snoozed tickets.

    Before this change the failure was silent and stored the answer anyway. It is now a
    typed refusal — better, but still a refusal. If a future creator forgets mark_proposed,
    this test says where to look.
    """
    service, manager, _ = _service(monkeypatch, state="pending")
    result = service.respond("t1", "done", label="Done")
    assert result.success is False
    assert result.not_answerable is True
    assert manager.ticket.user_text is None


def test_the_predicate_matches_the_write_path_on_same_state():
    """_transition_state_in_session treats X -> X as success, so can_transition must too;
    otherwise a repeated click would start reporting a false failure."""
    assert TicketManager.can_transition("expired", TicketState.EXPIRED) is True
    assert TicketManager.can_transition("proposed", TicketState.ACCEPTED) is True
    assert TicketManager.can_transition("expired", TicketState.ACCEPTED) is False
