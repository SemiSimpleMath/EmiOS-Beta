"""
Dayflow ticket response feedback.

When a user accepts/declines/snoozes a ticket, stamps the response
onto the source plan tasks that triggered the ticket and transitions
them to the appropriate lifecycle state (acted_on, suppressed, waiting).

Tickets are delivery mechanisms, not dayflow items — only the source
tasks are mutated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
from app.assistant.dayflow_orchestrator.orchestrator_status import DAYFLOW_ORCHESTRATOR_ROOM_ID
from app.assistant.dayflow_orchestrator.state_store import (
    RESOLVED_STATES,
    write_action_log,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_ticket_response_feedback(
        *,
        existing_items: List[Dict[str, Any]],
        since_utc: datetime,
) -> int:
    """
    When a user accepts/declines/snoozes a ticket, stamp the response onto
    the source plan tasks referenced in ``trigger_context.source_task_ids``
    and transition them to the appropriate state.

    Returns count of items mutated.
    """
    from app.assistant.ticket_manager import get_ticket_manager, TicketState

    responded = get_ticket_manager().get_tickets(
        since_responded_utc=since_utc,
        states=[TicketState.ACCEPTED, TicketState.DISMISSED, TicketState.SNOOZED],
        order_by="responded_at",
        limit=50,
    )
    logger.info(
        "apply_ticket_response_feedback: query since_utc=%s returned %d ticket(s).",
        since_utc.isoformat() if since_utc else "None",
        len(responded) if responded else 0,
    )
    if not responded:
        return 0

    existing_by_id: Dict[str, Dict[str, Any]] = {}
    action_logs_by_ticket: Dict[str, List[Dict[str, Any]]] = {}
    for item in existing_items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        item_id = str(meta.get("item_id") or item.get("id") or "").strip()
        if item_id:
            existing_by_id[item_id] = item
        # Also key by short_id so source_task_ids references (which may be
        # short IDs from the action_selector) resolve correctly.
        short_id = str(meta.get("short_id") or "").strip()
        if short_id:
            existing_by_id[short_id] = item
        # Index action log entries by created_ticket_id so we can
        # retroactively update their outcome with the user's reply.
        cti = str(meta.get("created_ticket_id") or "").strip()
        if cti and str(meta.get("source_type") or "").strip() == "action_result":
            action_logs_by_ticket.setdefault(cti, []).append(item)

    mutations: List[Dict[str, Any]] = []

    for ticket in responded:
        ticket_id = str(getattr(ticket, "ticket_id", "") or "").strip()
        if not ticket_id:
            logger.warning("apply_ticket_response_feedback: skipping ticket with empty ticket_id.")
            continue

        action = str(getattr(ticket, "user_action", "") or "").strip().lower()
        user_text = str(getattr(ticket, "user_text", "") or "").strip()
        snooze_until = getattr(ticket, "snooze_until", None)

        if action not in (
            "done", "willdo", "accept", "accepted", "acknowledge",
            "skip", "no", "decline", "declined", "dismiss", "dismissed",
            "later", "snooze", "snoozed",
        ):
            logger.warning(
                "apply_ticket_response_feedback: ticket %s has unrecognized action=%r, skipping.",
                ticket_id, action,
            )
            continue

        logger.info(
            "apply_ticket_response_feedback: processing ticket %s — action=%s, user_text=%s",
            ticket_id, action, repr(user_text[:80]) if user_text else "(none)",
        )

        # Tickets are delivery mechanisms, not dayflow items.  The response
        # is stamped onto the source tasks that triggered the ticket.
        tc = getattr(ticket, "trigger_context", None) or {}
        if not isinstance(tc, dict):
            tc = {}
        source_task_ids = tc.get("source_task_ids")
        if not isinstance(source_task_ids, list) or not source_task_ids:
            logger.info(
                "apply_ticket_response_feedback: ticket %s has no source_task_ids in trigger_context. "
                "trigger_context=%s",
                ticket_id, tc,
            )
            continue

        logger.info(
            "apply_ticket_response_feedback: ticket %s has source_task_ids=%s",
            ticket_id, source_task_ids,
        )

        ticket_title = str(getattr(ticket, "title", "") or "").strip()
        response_summary = f"ticket:{action}"
        if user_text:
            response_summary += f" — {user_text}"
        if ticket_title:
            response_summary = f"[{ticket_title}] {response_summary}"

        for task_id in source_task_ids:
            task_id = str(task_id or "").strip()
            if not task_id:
                continue
            task_item = existing_by_id.get(task_id)
            if task_item is None:
                logger.warning(
                    "apply_ticket_response_feedback: source task %s NOT FOUND in existing_by_id. "
                    "Cannot stamp ticket_response.",
                    task_id,
                )
                continue
            task_meta = task_item.get("metadata")
            if not isinstance(task_meta, dict):
                continue
            # Skip if already processed — task moved to terminal state on a previous tick.
            current_state = str(task_meta.get("state") or "").strip().lower()

            if current_state in RESOLVED_STATES:
                logger.debug(
                    "apply_ticket_response_feedback: task %s already in state '%s', skipping.",
                    task_id, current_state,
                )
                continue
            # Patch ticket response fields onto the DB row directly.

            ticket_patch = {
                "ticket_response": response_summary,
                "ticket_response_action": action,
                "ticket_id": ticket_id,
                "execution_result": response_summary,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }
            if user_text:
                ticket_patch["ticket_response_text"] = user_text
            write_dayflow_item(task_id, updates=ticket_patch, caller="ticket_feedback")
            task_meta.update(ticket_patch)
            # Write action log message so the planner sees the response.
            # Use short_id if available so the log is readable.

            plan_id = str(task_meta.get("plan_id") or "").strip()
            display_task_id = str(task_meta.get("short_id") or "").strip() or task_id
            write_action_log(
                task_id=display_task_id,
                plan_id=plan_id,
                event_type="ticket_response",
                summary=f"User replied ({action}): {user_text or ticket_title}",
                detail=response_summary,
                idempotency_key=f"{task_id}|ticket_response|{ticket_id}",
            )
            # Move the source plan task to a terminal state so it doesn't
            # get re-dispatched. Accepted → closed, declined → suppressed.
            if action in ("done", "willdo", "accept", "accepted", "acknowledge"):
                mutations.append({
                    "item_id": task_id,
                    "from_state": "",
                    "to_state": "closed",
                    "reason": f"ticket_accepted:{ticket_id}",
                })
            elif action in ("skip", "no", "decline", "declined", "dismiss", "dismissed"):
                mutations.append({
                    "item_id": task_id,
                    "from_state": "",
                    "to_state": "suppressed",
                    "reason": f"ticket_declined:{ticket_id}",
                })
            elif action in ("later", "snooze", "snoozed"):
                mut: Dict[str, Any] = {
                    "item_id": task_id,
                    "from_state": "",
                    "to_state": "waiting",
                    "reason": f"ticket_snoozed:{ticket_id}",
                    "wait_reason": f"User snoozed ticket: {ticket_title}",
                }
                if snooze_until:
                    snooze_iso = snooze_until.isoformat() if hasattr(snooze_until, "isoformat") else str(snooze_until)
                    mut["reactivate_at_utc"] = snooze_iso
                mutations.append(mut)
            logger.info(
                "apply_ticket_response_feedback: patched ticket_response onto source task %s "
                "(response=%s).",
                task_id, repr(response_summary[:80]),
            )

        # Update action log entries that created this ticket — replace
        # "Created ticket..." with the actual user response.
        action_log_items = action_logs_by_ticket.get(ticket_id, [])
        for al_item in action_log_items:
            al_meta = al_item.get("metadata")
            if isinstance(al_meta, dict):
                al_id = str(al_meta.get("item_id") or al_item.get("id") or "").strip()
                if al_id:
                    write_dayflow_item(al_id, updates={"outcome": response_summary}, caller="ticket_feedback")
                    al_meta["outcome"] = response_summary
                    logger.info(
                        "apply_ticket_response_feedback: patched action_log %s outcome for ticket %s.",
                        al_id, ticket_id,
                    )

    if not mutations:
        logger.info("apply_ticket_response_feedback: no state mutations to apply.")
        return 0

    # Apply state mutations via write_dayflow_item (validates transitions).
    # Ticket response fields were already patched above.
    applied = 0
    for mut in mutations:
        item_id = str(mut.get("item_id") or "").strip()
        to_state = str(mut.get("to_state") or "").strip()
        reason = str(mut.get("reason") or "").strip()
        if not item_id or not to_state:
            continue
        # Collect extended mutation keys into an updates dict.
        extra_updates: Dict[str, Any] = {}
        for ext_key in ("wait_reason", "reactivate_at_utc", "priority_on_wake"):
            val = mut.get(ext_key)
            if val is not None and val != "":
                extra_updates[ext_key] = val
        for list_key in ("wake_signals", "blocked_by"):
            val = mut.get(list_key)
            if isinstance(val, list) and val:
                extra_updates[list_key] = val
        try:
            write_dayflow_item(
                item_id,
                updates=extra_updates or None,
                state=to_state,
                reason=reason,
                caller="ticket_feedback",
            )
            applied += 1
        except ValueError as e:
            logger.warning(
                "apply_ticket_response_feedback: skipping mutation for %s: %s",
                item_id, e,
            )
    logger.info(
        "apply_ticket_response_feedback: DONE — %d state mutation(s) applied.",
        applied,
    )
    return applied
