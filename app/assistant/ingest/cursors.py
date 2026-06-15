"""Cursor persistence for gut sources.

Each source stores its last-seen position (message id, timestamp, etc.)
under a ``source_key`` of its choice. The store is a trivial key→value
backed by the ``ingest_cursor`` table.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.assistant.ingest.models import IngestCursorRow
from app.assistant.utils.logging_config import get_logger
from app.models.base import Base, get_session

logger = get_logger(__name__)


class IngestCursorStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        session = get_session()
        try:
            engine = session.bind
            Base.metadata.create_all(
                engine,
                tables=[IngestCursorRow.__table__],
                checkfirst=True,
            )
        finally:
            session.close()

    def get(self, source_key: str) -> Optional[str]:
        source_key = str(source_key or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        with self._lock:
            session = get_session()
            try:
                row = session.query(IngestCursorRow).filter_by(source_key=source_key).one_or_none()
                if row is None:
                    return None
                return str(row.cursor_value)
            finally:
                session.close()

    def set(self, *, source_key: str, cursor_value: str) -> None:
        source_key = str(source_key or "").strip()
        cursor_value = str(cursor_value or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        if not cursor_value:
            raise ValueError("cursor_value is required")
        # Serialize through the single application writer (db_manager) so the
        # frequent boot-time cursor writes queue instead of colliding on the lock.
        from app.models.db_manager import get_db_manager
        with self._lock:
            with get_db_manager().transaction(op="ingest_cursor.set") as session:
                row = session.query(IngestCursorRow).filter_by(source_key=source_key).one_or_none()
                if row is None:
                    row = IngestCursorRow(source_key=source_key)
                row.cursor_value = cursor_value
                session.add(row)

    def clear(self, source_key: str) -> None:
        """Delete a cursor row. Used by tests that need a clean starting state."""
        source_key = str(source_key or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        from app.models.db_manager import get_db_manager
        with self._lock:
            with get_db_manager().transaction(op="ingest_cursor.clear") as session:
                session.query(IngestCursorRow).filter_by(source_key=source_key).delete()
