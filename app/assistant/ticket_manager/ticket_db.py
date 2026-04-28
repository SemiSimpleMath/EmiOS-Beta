# ticket_db.py
# Database management functions for the tickets table.

"""
Ticket Database Management
==========================

Functions to initialize, drop, and reset the tickets table.
Call initialize_tickets_db() at app startup to ensure the table exists.

Usage:
    python ticket_db.py [init|drop|reset]
"""

from app.models.base import Base
from app.assistant.ticket_manager.ticket import Ticket


def initialize_tickets_db() -> None:
    """
    Initialize the tickets table if it does not exist.
    
    Call this at app startup (e.g., in bootstrap.py) before using TicketManager.
    Safe to call multiple times.
    """
    from sqlalchemy import inspect
    from app.models.base import get_session
    
    session = get_session()
    engine = session.bind
    print(f"🎫 Tickets DB: Connecting to database: {engine.url}")
    
    # Check if table already exists to avoid index creation errors
    inspector = inspect(engine)
    if "tickets" in inspector.get_table_names():
        print("Tickets table already exists, skipping creation.")
        session.close()
        return
    
    Base.metadata.create_all(engine, tables=[Ticket.__table__], checkfirst=True)
    session.close()
    print("Tickets table initialized successfully.")


def drop_tickets_db() -> None:
    """
    Drop the tickets table.
    
    WARNING: This will delete all ticket data!
    """
    from app.models.base import get_session
    
    session = get_session()
    engine = session.bind
    Base.metadata.drop_all(engine, tables=[Ticket.__table__], checkfirst=True)
    session.close()
    print("Tickets table dropped successfully.")


def reset_tickets_db() -> None:
    """
    Drop and recreate the tickets table.
    
    WARNING: This will delete all ticket data!
    """
    print("Resetting tickets database...")
    drop_tickets_db()
    initialize_tickets_db()
    print("Tickets database reset completed.")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "drop":
            drop_tickets_db()
        elif command == "reset":
            reset_tickets_db()
        elif command == "init":
            initialize_tickets_db()
        else:
            print("Usage: python ticket_db.py [init|drop|reset]")
            print("  init  - Create tickets table if not exists")
            print("  drop  - Drop tickets table")
            print("  reset - Drop and recreate tickets table")
    else:
        initialize_tickets_db()
