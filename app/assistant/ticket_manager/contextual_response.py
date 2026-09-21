"""Atomic response receipts for Dayflow's contextual choices; never executes actions."""
from app.assistant.ticket_manager.ticket import Ticket, TicketState
from app.assistant.ticket_manager.response_choices import validate_choices


def record_response(manager, ticket_id, choice_id, typed_text):
    now = manager._now_utc()
    with manager._session_scope(immediate=True) as session:
        ticket = session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        if (ticket is None or ticket.ticket_type != "dayflow_orchestrator"
                or ticket.action_type not in (None, "", "none") or ticket.status_effect):
            raise ValueError("Contextual responses require a Dayflow ticket without automatic side effects")
        choices = validate_choices((ticket.trigger_context or {}).get("response_choices", []))
        selected = next((c for c in choices if c["id"] == choice_id), None)
        if choice_id and selected is None:
            raise ValueError("Unknown choice for this ticket")
        if not selected and not typed_text.strip():
            raise ValueError("A written answer or a choice is required")
        receipt = {"choice_id": choice_id or "", "label": selected["label"] if selected else "",
                   "meaning": selected["meaning"] if selected else "answer",
                   "scope": selected["scope"] if selected else ticket.message,
                   "typed_text": typed_text, "question": ticket.message}
        prior = dict(ticket.user_response_parsed or {})
        history = list(prior.get("response_history") or [])
        if history and all(history[-1].get(k) == v for k, v in receipt.items()):
            return {"duplicate": True, "state": ticket.state, "action": ticket.user_action,
                    "user_text": ticket.user_text}
        # Follow-ups preserve lifecycle and execution state. They are new evidence, not a rerun.
        followup = bool(history) and ticket.state in {"accepted", "completed", "dismissed", "snoozed"}
        if not followup and ticket.state != "proposed":
            return None
        if not followup and ticket.valid_until:
            due = ticket.valid_until
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
            if due <= now:
                return None
        receipt["responded_at"] = now.isoformat()
        receipt["followup"] = followup
        history.append(receipt)
        # All contextual choices are answers. Permission and completion remain scoped evidence
        # for the finalizer; accepted here means only that a response was recorded.
        action = "acknowledge" if receipt["meaning"] == "acknowledge" and not typed_text.strip() else "answer"
        combined = typed_text if typed_text.strip() else receipt["label"]
        fields = {"user_text": combined, "user_action": action,
                  "user_response_parsed": {"decision": "response", **receipt, "response_history": history}}
        if not followup:
            if not manager._transition_state_in_session(session, ticket, TicketState.ACCEPTED,
                    "Contextual user response", now, fields):
                return None
        else:
            for key, value in fields.items():
                setattr(ticket, key, value)
            ticket.responded_at = now
            ticket.updated_at = now
        return {"duplicate": False, "state": ticket.state, "action": action, "user_text": combined}
