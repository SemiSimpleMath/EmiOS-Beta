"""
Ticket Manager
===============

Generic ticket system for managing suggestions from any agent.

Components:
- TicketManager: Database CRUD, state transitions
- TicketService: Handles user responses (accept/dismiss/snooze)
- Ticket: ORM model
- TicketState: State enum (pending, proposed, accepted, dismissed, etc.)
- TicketType: Master category enum (cron_reminder, tool_approval, dayflow_*)
- initialize_tickets_db: Call at app startup to ensure table exists
- drop_tickets_db, reset_tickets_db: Database management utilities
"""

from app.assistant.ticket_manager.ticket_manager import (
    TicketManager,
    get_ticket_manager,
)

from app.assistant.ticket_manager.ticket_service import (
    TicketService,
    TicketResponse,
    get_ticket_service,
)

from app.assistant.ticket_manager.ticket import (
    Ticket,
    TicketState,
    TicketType,
)

from app.assistant.ticket_manager.ticket_db import (
    initialize_tickets_db,
    drop_tickets_db,
    reset_tickets_db,
)

__all__ = [
    'TicketManager',
    'get_ticket_manager',
    'TicketService',
    'TicketResponse',
    'get_ticket_service',
    'Ticket',
    'TicketState',
    'TicketType',
    'initialize_tickets_db',
    'drop_tickets_db',
    'reset_tickets_db',
]
