# Note for Coding agents: Do not change this file without permission from the user.

"""
TicketManager
==============
Generic CRUD and state transition system for tickets.

This is a type-agnostic ticket system. The ticket flow is:
    pending -> proposed -> accepted/dismissed/snoozed/expired

Callers are responsible for:
- Providing explicit ticket_type when creating tickets
- Emitting to UI if needed (not handled here)
- Processing side effects when tickets are accepted
- Policy decisions (e.g., what to do with expired snoozed tickets)

NOTE: DB table must be initialized at app startup via initialize_tickets_db().
      TicketManager assumes the table already exists.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Union, FrozenSet

from sqlalchemy import func, text, and_, or_

from app.models.base import get_session
from app.assistant.ticket_manager.ticket import Ticket, TicketState
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# Fields that can be updated via update_ticket()
# Excludes: state, state_history, timestamps (use transition_state for those)
_UPDATABLE_FIELDS: FrozenSet[str] = frozenset({
    "title",
    "message",
    "action_type",
    "action_params",
    "trigger_reason",
    "valid_until",
    "status_effect",
})

# Valid order_by fields for get_tickets()
_ORDERABLE_FIELDS: FrozenSet[str] = frozenset({
    "created_at",
    "responded_at",
    "proposed_at",
    "updated_at",
    "valid_until",
})

# Terminal states, tickets cannot transition out of these states
_TERMINAL_STATES: FrozenSet[str] = frozenset({
    TicketState.COMPLETED.value,
    TicketState.DISMISSED.value,
    TicketState.EXPIRED.value,
    TicketState.FAILED.value,
})

# States that can be bulk-expired (display states, not in-progress states)
_EXPIRABLE_STATES: FrozenSet[str] = frozenset({
    TicketState.PENDING.value,
    TicketState.PROPOSED.value,
    TicketState.SNOOZED.value,
})

# Fields allowed as kwargs in transition_state()
# Restricts what callers can modify through state transitions
_TRANSITION_ALLOWED_KWARGS: FrozenSet[str] = frozenset({
    "snooze_until",
    "snooze_count",
    "user_action",
    "user_text",
    "user_response_parsed",
    "execution_result",
    "related_action_id",
    "ask_user_id",
    "auto_resolved_at",
    "resolution_reason",
    "assumed_status_effect",
})

# Valid state transitions: from_state -> allowed to_states
# Terminal states have empty sets.
_ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    TicketState.PENDING.value: frozenset({
        TicketState.PROPOSED.value,
        TicketState.EXPIRED.value,
    }),
    TicketState.PROPOSED.value: frozenset({
        TicketState.ACCEPTED.value,
        TicketState.SNOOZED.value,
        TicketState.DISMISSED.value,
        TicketState.EXPIRED.value,
    }),
    TicketState.SNOOZED.value: frozenset({
        TicketState.PROPOSED.value,
        TicketState.EXPIRED.value,
    }),
    TicketState.ACCEPTED.value: frozenset({
        TicketState.EXECUTING.value,
        TicketState.COMPLETED.value,
        TicketState.FAILED.value,
    }),
    TicketState.EXECUTING.value: frozenset({
        TicketState.COMPLETED.value,
        TicketState.FAILED.value,
    }),
    TicketState.COMPLETED.value: frozenset(),
    TicketState.DISMISSED.value: frozenset(),
    TicketState.EXPIRED.value: frozenset(),
    TicketState.FAILED.value: frozenset(),
}


# =============================================================================
# TicketManager
# =============================================================================

class TicketManager:
    """
    Manages ticket lifecycle.

    Responsibilities:
    - Create new tickets
    - Query tickets by state
    - Transition ticket states (with invariant enforcement)
    - Bulk operations (clear, count, expire)

    NOT responsible for:
    - Policy decisions (what to do with stale/snoozed tickets)
    - UI emission
    - Side effects on accept

    State machine invariants:
    - Tickets in terminal states cannot transition out
    - Only display states (pending, proposed, snoozed) can be bulk-expired
    - Valid transitions are enforced by _ALLOWED_TRANSITIONS

    NOTE: Assumes tickets table exists. Call initialize_tickets_db() at app startup.
    """

    def __init__(self) -> None:
        pass

    @contextmanager
    def _session_scope(self, immediate: bool = False):
        """
        Session context manager.

        Args:
            immediate: If True, starts with BEGIN IMMEDIATE for SQLite write lock.
                      IMPORTANT: Do not perform any DB work before calling with
                      immediate=True, or you may get "cannot start a transaction
                      within a transaction" errors.
        """
        session = get_session()
        session.expire_on_commit = False
        try:
            if immediate:
                # SQLite only. If you run this on Postgres, it will error.
                session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    # =========================================================================
    # Claim Operations (for processing accepted tickets)
    # =========================================================================

    def claim_accepted_tickets(
            self,
            ticket_type: Optional[str] = None,
            batch_size: int = 10,
    ) -> List[Ticket]:
        """
        Atomically claim accepted, unprocessed tickets for processing.

        Uses a claim_token to ensure atomic cross-process claiming.

        Returns:
            List of claimed Ticket objects
        """
        claim_token = uuid.uuid4().hex
        now = self._now_utc()

        with self._session_scope(immediate=True) as session:
            subq = session.query(Ticket.id).filter(
                Ticket.state == TicketState.ACCEPTED.value,
                Ticket.effects_processed == 0,
                Ticket.claim_token.is_(None),
                )
            if ticket_type:
                subq = subq.filter(Ticket.ticket_type == ticket_type)

            # Deterministic ordering: oldest responded first, then oldest created.
            # SQLite NULL ordering is tricky, the boolean trick makes it stable.
            subq = subq.order_by(
                Ticket.responded_at.is_(None).asc(),
                Ticket.responded_at.asc(),
                Ticket.created_at.asc(),
            ).limit(batch_size).subquery()

            claimed_count = session.query(Ticket).filter(
                Ticket.id.in_(session.query(subq.c.id)),
                Ticket.state == TicketState.ACCEPTED.value,
                Ticket.effects_processed == 0,
                Ticket.claim_token.is_(None),
                ).update(
                {
                    Ticket.effects_processed: 2,
                    Ticket.claim_token: claim_token,
                    Ticket.claimed_at: now,
                },
                synchronize_session=False,
            )

            if claimed_count == 0:
                return []

            claimed = session.query(Ticket).filter(
                Ticket.claim_token == claim_token,
                Ticket.effects_processed == 2,
                Ticket.state == TicketState.ACCEPTED.value,
                ).all()

            logger.debug(f"Claimed {len(claimed)} tickets with token {claim_token[:8]}...")
            return claimed

    def mark_ticket_processed(self, ticket_id: int, clear_claim: bool = True) -> None:
        """
        Mark a claimed ticket as fully processed.

        Args:
            ticket_id: The ticket's primary key (id, not ticket_id string)
            clear_claim: If True, also clears claim_token and claimed_at
        """
        with self._session_scope() as session:
            updates: Dict[Any, Any] = {Ticket.effects_processed: 1, Ticket.updated_at: self._now_utc()}
            if clear_claim:
                updates[Ticket.claim_token] = None
                updates[Ticket.claimed_at] = None
            session.query(Ticket).filter(Ticket.id == ticket_id).update(updates, synchronize_session=False)

    def release_claim(self, ticket_id: int) -> None:
        """
        Release a claim without marking processed (e.g., on error).

        Args:
            ticket_id: The ticket's primary key
        """
        with self._session_scope() as session:
            session.query(Ticket).filter(Ticket.id == ticket_id).update(
                {
                    Ticket.effects_processed: 0,
                    Ticket.claim_token: None,
                    Ticket.claimed_at: None,
                    Ticket.updated_at: self._now_utc(),
                },
                synchronize_session=False,
            )

    # =========================================================================
    # Create Operations
    # =========================================================================

    def create_ticket(
            self,
            ticket_type: str,
            suggestion_type: str,
            title: str,
            message: str,
            action_type: str = "none",
            action_params: Optional[Dict] = None,
            trigger_context: Optional[Dict] = None,
            trigger_reason: Optional[str] = None,
            valid_hours: int = 4,
            status_effect: Optional[List[str]] = None,
    ) -> Optional[Ticket]:
        """
        Create a new ticket in PENDING state.

        NOTE: This only creates the ticket. Caller is responsible for:
        - Calling mark_proposed() if ticket should be shown to user
        - Emitting to UI if needed
        """
        now = self._now_utc()
        ticket_id = f"{ticket_type}_{suggestion_type}_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        with self._session_scope() as session:
            ticket = Ticket(
                ticket_id=ticket_id,
                ticket_type=ticket_type,
                suggestion_type=suggestion_type,
                state=TicketState.PENDING.value,
                state_history=[{
                    "from_state": None,
                    "to_state": TicketState.PENDING.value,
                    "timestamp": now.isoformat(),
                    "reason": "Created",
                }],
                title=title,
                message=message,
                action_type=action_type,
                action_params=action_params or {},
                trigger_context=trigger_context or {},
                trigger_reason=trigger_reason,
                status_effect=status_effect or [],
                effects_processed=0,
                valid_from=now,
                valid_until=now + timedelta(hours=valid_hours),
            )
            session.add(ticket)
            logger.info(f"📋 Created ticket: {ticket_id} (type={ticket_type})")
            return ticket

    # =========================================================================
    # Query Operations
    # =========================================================================

    def get_tickets(
            self,
            *,
            since_utc: Optional[datetime] = None,
            until_utc: Optional[datetime] = None,
            since_responded_utc: Optional[datetime] = None,
            state: Optional[Union[TicketState, str]] = None,
            states: Optional[List[Union[TicketState, str]]] = None,
            exclude_terminal: bool = False,
            ticket_type: Optional[str] = None,
            suggestion_type: Optional[str] = None,
            effects_processed: Optional[int] = None,
            limit: int = 100,
            order_by: str = "created_at",
            order_desc: bool = True,
    ) -> List[Ticket]:
        """
        Unified ticket query with flexible filtering.
        """
        if order_by not in _ORDERABLE_FIELDS:
            raise ValueError(
                f"Invalid order_by field '{order_by}'. "
                f"Valid fields: {', '.join(sorted(_ORDERABLE_FIELDS))}"
            )

        with self._session_scope() as session:
            query = session.query(Ticket)

            if since_utc:
                query = query.filter(Ticket.created_at >= since_utc)
            if until_utc:
                query = query.filter(Ticket.created_at <= until_utc)

            if since_responded_utc:
                query = query.filter(
                    Ticket.responded_at.isnot(None),
                    Ticket.responded_at > since_responded_utc,
                    )

            if state:
                state_val = state.value if isinstance(state, TicketState) else state
                query = query.filter(Ticket.state == state_val)
            elif states:
                state_vals = [s.value if isinstance(s, TicketState) else s for s in states]
                query = query.filter(Ticket.state.in_(state_vals))
            elif exclude_terminal:
                query = query.filter(~Ticket.state.in_(_TERMINAL_STATES))

            if ticket_type:
                query = query.filter(Ticket.ticket_type == ticket_type)
            if suggestion_type:
                query = query.filter(Ticket.suggestion_type == suggestion_type)

            if effects_processed is not None:
                query = query.filter(Ticket.effects_processed == effects_processed)

            order_field = getattr(Ticket, order_by)
            query = query.order_by(order_field.desc() if order_desc else order_field.asc())

            return query.limit(limit).all()

    def get_ticket_by_id(self, ticket_id: str) -> Optional[Ticket]:
        """Get a ticket by its ticket_id string."""
        with self._session_scope() as session:
            return session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()

    def update_ticket(self, ticket_id: str, **kwargs) -> bool:
        """
        Update allowed fields on a ticket.

        State changes must go through transition_state().
        """
        restricted = set(kwargs.keys()) - _UPDATABLE_FIELDS
        if restricted:
            raise ValueError(
                f"Cannot update restricted fields via update_ticket(): {restricted}. "
                f"Use transition_state() for state changes."
            )

        with self._session_scope() as session:
            ticket = session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if not ticket:
                logger.warning(f"Ticket not found for update: {ticket_id}")
                return False

            for field, value in kwargs.items():
                setattr(ticket, field, value)

            ticket.updated_at = self._now_utc()
            logger.debug(f"Updated ticket {ticket_id}: {list(kwargs.keys())}")
            return True

    def save_ticket(self, ticket: Ticket) -> bool:
        """Save/update a ticket's changes to the database."""
        with self._session_scope() as session:
            session.merge(ticket)
            return True

    def count_tickets(
            self,
            *,
            state: Optional[Union[TicketState, str]] = None,
            states: Optional[List[Union[TicketState, str]]] = None,
            exclude_terminal: bool = False,
            ticket_type: Optional[str] = None,
            suggestion_type: Optional[str] = None,
    ) -> int:
        """Count tickets matching filters (efficient DB COUNT)."""
        with self._session_scope() as session:
            query = session.query(func.count(Ticket.id))

            if state:
                state_val = state.value if isinstance(state, TicketState) else state
                query = query.filter(Ticket.state == state_val)
            elif states:
                state_vals = [s.value if isinstance(s, TicketState) else s for s in states]
                query = query.filter(Ticket.state.in_(state_vals))
            elif exclude_terminal:
                query = query.filter(~Ticket.state.in_(_TERMINAL_STATES))

            if ticket_type:
                query = query.filter(Ticket.ticket_type == ticket_type)
            if suggestion_type:
                query = query.filter(Ticket.suggestion_type == suggestion_type)

            return query.scalar() or 0

    def has_similar_active_ticket(self, suggestion_type: str, ticket_type: Optional[str] = None) -> bool:
        """
        Check if there's already an active (non-terminal) ticket with same suggestion_type.
        """
        return self.count_tickets(
            suggestion_type=suggestion_type,
            ticket_type=ticket_type,
            exclude_terminal=True,
        ) > 0

    def get_snoozed_tickets_ready(self) -> List[Ticket]:
        """Get snoozed tickets whose snooze time has passed."""
        now = self._now_utc()
        with self._session_scope() as session:
            return session.query(Ticket).filter(
                Ticket.state == TicketState.SNOOZED.value,
                Ticket.snooze_until.isnot(None),
                Ticket.snooze_until <= now,
                ).all()

    def get_tickets_pending_or_proposed(
            self,
            *,
            exclude_snoozed: bool = True,
            max_age_hours: Optional[float] = None,
    ) -> List[Ticket]:
        """
        Get tickets in PENDING or PROPOSED state for display.

        Important: this is driven by valid_until. The optional max_age_hours is
        only a debug or safety lever, and defaults to None to avoid silently
        hiding still-valid tickets.
        """
        now = self._now_utc()

        with self._session_scope() as session:
            query = session.query(Ticket).filter(
                Ticket.state.in_([TicketState.PENDING.value, TicketState.PROPOSED.value]),
                or_(Ticket.valid_until.is_(None), Ticket.valid_until > now),
            )

            if exclude_snoozed:
                query = query.filter(or_(Ticket.snooze_until.is_(None), Ticket.snooze_until <= now))

            if max_age_hours is not None:
                min_created = now - timedelta(hours=max_age_hours)
                query = query.filter(Ticket.created_at >= min_created)

            return query.order_by(Ticket.created_at.asc()).all()

    # =========================================================================
    # State Transition Operations
    # =========================================================================

    def _transition_state_in_session(
            self,
            session,
            ticket: Ticket,
            new_state: TicketState,
            reason: Optional[str],
            now: datetime,
            transition_kwargs: Dict[str, Any],
    ) -> bool:
        old_state = ticket.state

        if old_state == new_state.value:
            return True

        allowed = _ALLOWED_TRANSITIONS.get(old_state, frozenset())
        if new_state.value not in allowed:
            logger.warning(
                f"Invalid transition for ticket {ticket.ticket_id}: "
                f"'{old_state}' -> '{new_state.value}' not allowed. "
                f"Valid targets: {sorted(allowed) if allowed else 'none (terminal state)'}"
            )
            return False

        ticket.state = new_state.value

        history = list(ticket.state_history or [])
        history.append({
            "from_state": old_state,
            "to_state": new_state.value,
            "timestamp": now.isoformat(),
            "reason": reason or f"Transitioned to {new_state.value}",
        })
        ticket.state_history = history

        ticket.updated_at = now

        if new_state == TicketState.PROPOSED:
            ticket.proposed_at = now
        elif new_state in (TicketState.ACCEPTED, TicketState.SNOOZED, TicketState.DISMISSED):
            ticket.responded_at = now
        elif new_state in (TicketState.COMPLETED, TicketState.FAILED):
            # If your Ticket model later gains failed_at, prefer that for FAILED.
            ticket.completed_at = now

        for key, value in transition_kwargs.items():
            if not hasattr(ticket, key):
                raise ValueError(
                    f"Kwarg '{key}' is in _TRANSITION_ALLOWED_KWARGS but not on Ticket model. "
                    f"This is a configuration bug."
                )
            setattr(ticket, key, value)

        if new_state == TicketState.ACCEPTED:
            logger.info(
                "✅ Ticket accepted",
                extra={
                    "ticket_id": ticket.ticket_id,
                    "suggestion_type": ticket.suggestion_type,
                    "ticket_type": ticket.ticket_type,
                },
            )

        logger.info(f"📋 Ticket {ticket.ticket_id}: {old_state} -> {new_state.value}")
        return True

    def transition_state(
            self,
            ticket_id: str,
            new_state: TicketState,
            reason: Optional[str] = None,
            **kwargs,
    ) -> bool:
        """
        Transition a ticket to a new state.

        Enforces state machine invariants via _ALLOWED_TRANSITIONS.
        Only kwargs in _TRANSITION_ALLOWED_KWARGS are accepted, and they must exist
        as attributes on the Ticket model.
        """
        invalid_kwargs = set(kwargs.keys()) - _TRANSITION_ALLOWED_KWARGS
        if invalid_kwargs:
            raise ValueError(
                f"Invalid kwargs for transition_state(): {invalid_kwargs}. "
                f"Allowed: {sorted(_TRANSITION_ALLOWED_KWARGS)}"
            )

        now = self._now_utc()
        with self._session_scope() as session:
            ticket = session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if not ticket:
                logger.warning(f"Ticket not found: {ticket_id}")
                return False

            return self._transition_state_in_session(
                session=session,
                ticket=ticket,
                new_state=new_state,
                reason=reason,
                now=now,
                transition_kwargs=kwargs,
            )

    def mark_proposed(self, ticket_id: str, ask_user_id: Optional[str] = None) -> bool:
        return self.transition_state(
            ticket_id,
            TicketState.PROPOSED,
            reason="Proposed to user",
            ask_user_id=ask_user_id,
        )

    def mark_accepted(self, ticket_id: str, user_text: Optional[str] = None) -> bool:
        return self.transition_state(
            ticket_id,
            TicketState.ACCEPTED,
            reason="User accepted",
            user_text=user_text,
            user_response_parsed={"decision": "accept"},
        )

    def mark_snoozed(
            self,
            ticket_id: str,
            snooze_minutes: int = 30,
            user_text: Optional[str] = None,
    ) -> bool:
        """
        Mark ticket as snoozed by user.

        This is done in a single transaction so snooze_count is not lost to a race.
        """
        now = self._now_utc()
        snooze_until = now + timedelta(minutes=snooze_minutes)

        invalid_kwargs = {"snooze_until", "snooze_count", "user_text", "user_response_parsed"} - _TRANSITION_ALLOWED_KWARGS
        if invalid_kwargs:
            raise ValueError("Transition allowlist is missing required snooze fields.")

        with self._session_scope() as session:
            ticket = session.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if not ticket:
                logger.warning(f"Ticket not found: {ticket_id}")
                return False

            snooze_count = (ticket.snooze_count or 0) + 1

            return self._transition_state_in_session(
                session=session,
                ticket=ticket,
                new_state=TicketState.SNOOZED,
                reason=f"User snoozed for {snooze_minutes} minutes",
                now=now,
                transition_kwargs={
                    "snooze_until": snooze_until,
                    "snooze_count": snooze_count,
                    "user_text": user_text,
                    "user_response_parsed": {"decision": "snooze", "snooze_minutes": snooze_minutes},
                },
            )

    def mark_dismissed(self, ticket_id: str, user_text: Optional[str] = None) -> bool:
        return self.transition_state(
            ticket_id,
            TicketState.DISMISSED,
            reason="User dismissed",
            user_text=user_text,
            user_response_parsed={"decision": "dismiss"},
        )

    def mark_executing(self, ticket_id: str) -> bool:
        return self.transition_state(ticket_id, TicketState.EXECUTING, reason="Executing action")

    def mark_completed(
            self,
            ticket_id: str,
            execution_result: Optional[str] = None,
            related_action_id: Optional[str] = None,
    ) -> bool:
        return self.transition_state(
            ticket_id,
            TicketState.COMPLETED,
            reason="Action completed",
            execution_result=execution_result,
            related_action_id=related_action_id,
        )

    def mark_failed(self, ticket_id: str, execution_result: Optional[str] = None) -> bool:
        return self.transition_state(
            ticket_id,
            TicketState.FAILED,
            reason="Action failed",
            execution_result=execution_result,
        )

    def mark_expired(self, ticket_id: str, reason: str = "Time window passed") -> bool:
        return self.transition_state(ticket_id, TicketState.EXPIRED, reason=reason)

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    def expire_old_tickets(self, *, max_age_hours: Optional[float] = None) -> int:
        """
        Bulk expire display-state tickets that have passed valid_until.

        Optional max_age_hours is a safety lever. It is off by default to avoid
        expiring still-valid tickets.
        """
        now = self._now_utc()

        with self._session_scope() as session:
            conditions = [
                Ticket.state.in_(_EXPIRABLE_STATES),
                ~Ticket.state.in_(_TERMINAL_STATES),
                and_(
                    Ticket.valid_until.isnot(None),
                    Ticket.valid_until < now,
                    ),
            ]

            if max_age_hours is not None:
                max_age_cutoff = now - timedelta(hours=max_age_hours)
                conditions.append(Ticket.created_at < max_age_cutoff)

            count = session.query(Ticket).filter(*conditions).update(
                {
                    Ticket.state: TicketState.EXPIRED.value,
                    Ticket.updated_at: now,
                },
                synchronize_session=False,
            )

            if count > 0:
                logger.info(f"Bulk expired {count} old tickets")
            return count

    def get_stats(self) -> Dict[str, Any]:
        with self._session_scope() as session:
            total = session.query(func.count(Ticket.id)).scalar() or 0
            stats: Dict[str, Any] = {"total": total}
            for state in TicketState:
                stats[state.value] = session.query(func.count(Ticket.id)).filter(
                    Ticket.state == state.value
                ).scalar() or 0
            return stats

    def clear_tickets(
            self,
            ticket_type: Optional[str] = None,
            states: Optional[List[Union[TicketState, str]]] = None,
    ) -> int:
        with self._session_scope() as session:
            query = session.query(Ticket)
            if ticket_type:
                query = query.filter(Ticket.ticket_type == ticket_type)
            if states:
                state_vals = [s.value if isinstance(s, TicketState) else s for s in states]
                query = query.filter(Ticket.state.in_(state_vals))

            deleted = query.delete(synchronize_session="fetch")
            if deleted > 0:
                logger.info(f"🧹 Cleared {deleted} tickets (type={ticket_type}, states={states})")
            return deleted

    def clear_all_tickets(self) -> int:
        return self.clear_tickets()


# =============================================================================
# Singleton
# =============================================================================

_ticket_manager: Optional[TicketManager] = None


def get_ticket_manager() -> TicketManager:
    """Get the singleton TicketManager instance."""
    global _ticket_manager
    if _ticket_manager is None:
        _ticket_manager = TicketManager()
    return _ticket_manager
