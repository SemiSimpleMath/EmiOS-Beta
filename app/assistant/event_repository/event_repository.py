# event_repository.py
"""
Event Repository Manager

Stores, queries, and syncs events (calendar, email, weather, etc.) in the
EventRepository DB table. Each event has:
  - record_id: composite unique key (e.g., event_id + occurrence)
  - event_id: external identifier from the source system
  - data: JSON payload
  - data_type: category (calendar, email, weather, todo_task, etc.)
  - data_hash: SHA-256 of the payload for change detection
"""

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.assistant.database.db_handler import EventRepository
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 0.5


class EventRepositoryManager:
    def __init__(self, session_factory=get_session):
        self.session_factory = session_factory
        self.tracked_events: Dict[str, set] = {}
        self._tracked_lock = threading.Lock()

    @staticmethod
    def _compute_hash(data: dict) -> str:
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def store_event(
        self,
        id: str,  # noqa: A002 — record_id, kept as 'id' for DB column compat
        event_data: dict,
        data_type: str,
        event_id: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """Store or update an event.

        Returns (record_id, updated) where updated is True on insert or data change.
        """
        if event_id is None:
            event_id = id

        if isinstance(event_data, BaseModel):
            event_data = event_data.model_dump()

        event_hash = self._compute_hash(event_data)

        last_exception: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            session = self.session_factory()
            try:
                existing = session.query(EventRepository).filter_by(id=id, event_id=event_id).one_or_none()

                if existing:
                    if existing.data_hash == event_hash:
                        updated = False
                    else:
                        existing.data = event_data
                        existing.data_hash = event_hash
                        session.commit()
                        updated = True
                else:
                    session.add(EventRepository(
                        id=id,
                        event_id=event_id,
                        data=event_data,
                        data_type=data_type,
                        data_hash=event_hash,
                    ))
                    session.commit()
                    updated = True

                with self._tracked_lock:
                    if data_type not in self.tracked_events:
                        self.tracked_events[data_type] = set()
                    self.tracked_events[data_type].add(id)

                return id, updated

            except OperationalError as e:
                session.rollback()
                if "database is locked" in str(e):
                    last_exception = e
                    delay = RETRY_DELAY_SECONDS * (2 ** attempt)
                    logger.warning(
                        "Database locked on attempt %d/%d for event id=%s. Retrying in %.2fs...",
                        attempt + 1, MAX_RETRIES, id, delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error("Error storing event: %s", e)
                raise
            except SQLAlchemyError as e:
                session.rollback()
                logger.error("Error storing event: %s", e)
                raise
            finally:
                session.close()

        logger.error("Failed to store event after %d retries: %s", MAX_RETRIES, last_exception)
        raise last_exception  # type: ignore[misc]

    def get_event_by_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve an event by id. Returns dict with data_type and data, or None."""
        session = self.session_factory()
        try:
            event = session.query(EventRepository).filter(EventRepository.id == event_id).one_or_none()
            if event:
                return {"data_type": event.data_type, "data": event.data}
            return None
        except SQLAlchemyError as e:
            logger.error("Error retrieving event by id: %s", e)
            raise
        finally:
            session.close()

    def search_events(self, data_type: Optional[str] = None, keyword: Optional[str] = None) -> str:
        """Search events by data_type and optional keyword. Returns JSON string.

        Note: keyword search uses SQLite JSON text matching. For structured
        queries, prefer get_new_events_typed().
        """
        session = self.session_factory()
        try:
            query = session.query(EventRepository)
            if data_type:
                query = query.filter(EventRepository.data_type == data_type)
            if keyword:
                # SQLite-compatible: cast JSON column to text and search.
                from sqlalchemy import cast, String
                query = query.filter(
                    cast(EventRepository.data, String).ilike(f"%{keyword}%")
                )
            events = query.all()
            logger.debug("Found %d events with filters data_type=%s, keyword=%s", len(events), data_type, keyword)
            return json.dumps([
                {"data_type": e.data_type, "data": e.data} for e in events
            ])
        except SQLAlchemyError as e:
            logger.error("Error searching events: %s", e)
            raise
        finally:
            session.close()

    def delete_event(self, event_id: str, data_type: str) -> bool:
        """Delete all events with the given external event_id and data_type."""
        session = self.session_factory()
        try:
            events = session.query(EventRepository).filter(
                EventRepository.data_type == data_type,
                EventRepository.event_id == event_id,
            ).all()
            if not events:
                return False
            for event in events:
                session.delete(event)
            session.commit()
            logger.debug("Deleted %d event(s) event_id=%s data_type=%s", len(events), event_id, data_type)
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error("Error deleting event: %s", e)
            raise
        finally:
            session.close()

    def get_last_altered_by_data_type(self) -> Dict[str, Any]:
        """Return {data_type: max(created_at)} for each data type."""
        from sqlalchemy import func
        session = self.session_factory()
        try:
            results = session.query(
                EventRepository.data_type,
                func.max(EventRepository.created_at).label("last_altered"),
            ).group_by(EventRepository.data_type).all()
            return {data_type: ts for data_type, ts in results}
        except SQLAlchemyError as e:
            logger.error("Error fetching last_altered_by_data_type: %s", e)
            raise
        finally:
            session.close()

    def get_new_events(self, category: str, since_dt: datetime) -> str:
        """Retrieve events created since since_dt. Returns JSON string.

        Prefer get_new_events_typed() for in-process callers.
        """
        return json.dumps(self.get_new_events_typed(category, since_dt))

    def get_new_events_typed(self, category: str, since_dt: datetime) -> List[Dict[str, Any]]:
        """Retrieve events created since since_dt as Python dicts."""
        session = self.session_factory()
        try:
            events = (
                session.query(EventRepository)
                .filter(
                    EventRepository.data_type == category,
                    EventRepository.created_at > since_dt,
                )
                .all()
            )
            return [{"data_type": e.data_type, "data": e.data} for e in events]
        except SQLAlchemyError as e:
            logger.error("Error fetching new events category=%s: %s", category, e)
            raise
        finally:
            session.close()

    def sync_events_with_server(self, server_events: List[str], data_type: str) -> None:
        """Reconcile local events against server state and notify UI.

        For calendar/todo: delete local events whose IDs are not in server_events.
        For other types: delete events older than 10 hours (sliding window).

        Only publishes a repo_update event when something actually changed.

        Args:
            server_events: List of event IDs currently on the server.
            data_type: Event category to sync.
        """
        changed = False
        session = self.session_factory()
        try:
            if data_type in {"calendar", "todo_task"}:
                server_ids = set(server_events)
                db_events = session.query(EventRepository).filter(
                    EventRepository.data_type == data_type,
                ).all()
                db_ids = {event.id for event in db_events}
                missing_ids = db_ids - server_ids

                if missing_ids:
                    deleted = session.query(EventRepository).filter(
                        EventRepository.data_type == data_type,
                        EventRepository.id.in_(missing_ids),
                    ).delete(synchronize_session=False)
                    session.commit()
                    if deleted > 0:
                        logger.info("Cleaned up %d stale '%s' events", deleted, data_type)
                        changed = True
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=10)
                deleted = session.query(EventRepository).filter(
                    EventRepository.data_type == data_type,
                    EventRepository.created_at < cutoff,
                ).delete(synchronize_session=False)
                session.commit()
                if deleted > 0:
                    logger.info("Cleaned up %d old '%s' events", deleted, data_type)
                    changed = True

        except SQLAlchemyError as e:
            session.rollback()
            logger.error("Error syncing events: %s", e)
            raise
        finally:
            session.close()

        # Also check if store_event() recorded any new/updated events this cycle.
        with self._tracked_lock:
            tracked = self.tracked_events.pop(data_type, None)
        if tracked:
            changed = True

        if not changed:
            return

        repo_msg = Message(
            sender="repo_sync",
            receiver=None,
            data_type="agent_msg",
            content=data_type,
        )
        repo_msg.event_topic = "repo_update"
        DI.event_hub.publish(repo_msg)
