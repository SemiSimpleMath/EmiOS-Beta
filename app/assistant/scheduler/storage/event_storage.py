# event_storage.py
import threading
import json
from datetime import datetime, timezone
from typing import Optional

from app.assistant.database.db_instance import db
from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData
from sqlalchemy import Column, String, DateTime, Integer, JSON

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class TimeEvent(db.Model):
    __tablename__ = 'time_events'

    event_id = Column(String(255), primary_key=True)
    event_type = Column(String(50), nullable=False)
    interval = Column(Integer, nullable=True)
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    jitter = Column(Integer, nullable=True)
    event_payload = Column(JSON, nullable=True)

    def to_pydantic(self) -> BaseEventData:
        try:
            payload = self.event_payload
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:
            logger.error(f"Invalid JSON in event {self.event_id}: {e}")
            payload = {}

        # Ensure timezone-aware datetime strings
        start_date_str = None
        if self.start_date:
            dt = self.start_date
            if dt.tzinfo is None:
                logger.warning(f"Event {self.event_id} has naive start_date in DB, assuming UTC")
                dt = dt.replace(tzinfo=timezone.utc)
            start_date_str = dt.isoformat()
        
        end_date_str = None
        if self.end_date:
            dt = self.end_date
            if dt.tzinfo is None:
                logger.warning(f"Event {self.event_id} has naive end_date in DB, assuming UTC")
                dt = dt.replace(tzinfo=timezone.utc)
            end_date_str = dt.isoformat()

        return BaseEventData(
            event_id=self.event_id,
            event_type=self.event_type,
            interval=self.interval,
            start_date=start_date_str,
            end_date=end_date_str,
            jitter=self.jitter,
            event_payload=payload,
        )


class EventStorage:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        db.create_all()
        self.time_events = {}
        self.load_events()

    def load_events(self):
        logger.info("Loading events from database...")
        session = db.session

        try:
            now = datetime.now(timezone.utc)
            records = session.query(TimeEvent).all()

            for record in records:
                if record.start_date and record.start_date.tzinfo is None:
                    record.start_date = record.start_date.replace(tzinfo=timezone.utc)
                if record.end_date and record.end_date.tzinfo is None:
                    record.end_date = record.end_date.replace(tzinfo=timezone.utc)

                if record.event_type == "one_time_event" and record.start_date < now:
                    logger.debug(f"Skipping expired one-time event: {record.event_id}")
                    continue

                # Convert once while session is open
                pyd_event = record.to_pydantic()
                self.time_events[pyd_event.event_id] = pyd_event

            logger.info(f"Loaded {len(self.time_events)} events into memory.")

        except Exception as e:
            logger.error("Failed to load events: %s", e)
            logger.debug("failed to load events exception details", exc_info=True)
        finally:
            session.close()


    def add_time_event(self, event: BaseEventData):
        with self._lock:
            session = db.session
            try:
                record = session.query(TimeEvent).filter_by(event_id=event.event_id).first()
                if not record:
                    record = TimeEvent(event_id=event.event_id)

                record.event_type = event.event_type
                record.interval = event.interval
                record.jitter = event.jitter
                record.event_payload = event.event_payload

                # Handle start_date
                start_date_aware = None
                if event.start_date:
                    dt = event.start_date
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt)
                    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                    record.start_date = dt
                    start_date_aware = dt.isoformat()

                # Handle end_date
                end_date_aware = None
                if event.end_date:
                    dt = event.end_date
                    if isinstance(dt, str):
                        dt = datetime.fromisoformat(dt)
                    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                    record.end_date = dt
                    end_date_aware = dt.isoformat()

                session.add(record)
                session.commit()

                # Store timezone-aware version in memory cache
                event_with_tz = BaseEventData(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    interval=event.interval,
                    start_date=start_date_aware,
                    end_date=end_date_aware,
                    jitter=event.jitter,
                    event_payload=event.event_payload
                )
                self.time_events[event.event_id] = event_with_tz

                logger.info(f"Saved event: {event.event_id}")

            except Exception as e:
                session.rollback()
                logger.error("Failed to save event {event.event_id}: %s", e)
                logger.debug("failed to save event exception details", exc_info=True)
            finally:
                session.close()

    def get_time_event(self, event_id: str) -> Optional[BaseEventData]:
        with self._lock:
            record = self.time_events.get(event_id)
            return record if record else None

    def get_all_time_events(self):
        with self._lock:
            return list(self.time_events.values())


    def remove_event(self, event_id: str, scheduler=None) -> bool:
        with self._lock:
            session = db.session
            deleted = False

            try:
                record = session.query(TimeEvent).filter_by(event_id=event_id).first()
                if record:
                    session.delete(record)
                    session.commit()
                    deleted = True
                    logger.info(f"Deleted event {event_id} from database.")

                if event_id in self.time_events:
                    del self.time_events[event_id]
                    deleted = True
                    logger.info(f"Removed event {event_id} from in-memory store.")

                if scheduler:
                    try:
                        scheduler.remove_job(event_id)
                        logger.info(f"Removed job {event_id} from scheduler.")
                    except Exception as e:
                        logger.warning(f"Failed to remove job from scheduler: {e}")

            except Exception as e:
                session.rollback()
                logger.error("Failed to delete event {event_id}: %s", e)
                logger.debug("failed to delete event exception details", exc_info=True)
            finally:
                session.close()

            return deleted

    def clear_all_events(self):
        with self._lock:
            session = db.session
            try:
                session.query(TimeEvent).delete()
                session.commit()
                self.time_events.clear()
                logger.info("Cleared all events from storage.")
            except Exception as e:
                session.rollback()
                logger.error("Failed to clear all events.")
                logger.debug("failed to clear all events. exception details", exc_info=True)
            finally:
                session.close()
