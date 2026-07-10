# event_storage.py
import threading
import json
from datetime import datetime, timezone
from typing import Optional

from app.assistant.database.db_instance import db
from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData
from sqlalchemy import Column, String, DateTime, Integer, JSON

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import AwareUtcDateTime
logger = get_logger(__name__)


# How late a one-time event may still fire. A one-time reminder whose time passes while the
# process is DOWN (a deploy/restart/crash) is re-armed on boot with a now-past run_date; within
# this window it's kept and fired late (stamped 'overdue' by the timing engine so it isn't
# presented as on-time). Past it, the reminder can no longer usefully fire, so its row is deleted
# on load instead of lingering forever. Tune this to how stale a missed reminder is still worth
# surfacing — deploys are seconds-to-minutes; a longer window also covers a brief crash.
ONE_TIME_CATCHUP_GRACE_SECONDS = 15 * 60  # 15 minutes


def one_time_expired_beyond_grace(event_type, start_date, now, grace_seconds=ONE_TIME_CATCHUP_GRACE_SECONDS):
    """True when a one-time event is so far past its scheduled time that it can no longer usefully
    fire and its durable row should be deleted. Within the grace it's kept (caught up and fired
    late); a future event or a non-one-time event is never expired here. Pure — start_date and now
    must be tz-aware (or start_date None)."""
    if event_type != "one_time_event" or start_date is None:
        return False
    if start_date >= now:
        return False
    return (now - start_date).total_seconds() > grace_seconds


class TimeEvent(db.Model):
    __tablename__ = 'time_events'

    event_id = Column(String(255), primary_key=True)
    event_type = Column(String(50), nullable=False)
    interval = Column(Integer, nullable=True)
    start_date = Column(AwareUtcDateTime, nullable=True)
    end_date = Column(AwareUtcDateTime, nullable=True)
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
            expired_ids = []

            for record in records:
                if record.start_date and record.start_date.tzinfo is None:
                    record.start_date = record.start_date.replace(tzinfo=timezone.utc)
                if record.end_date and record.end_date.tzinfo is None:
                    record.end_date = record.end_date.replace(tzinfo=timezone.utc)

                if one_time_expired_beyond_grace(record.event_type, record.start_date, now):
                    # Past the catch-up window: it can no longer usefully fire, so drop it AND
                    # delete the row. (These were previously skipped but left in the table forever
                    # — a slow leak and a boot scan that grew without bound.)
                    expired_ids.append(record.event_id)
                    continue

                if record.event_type == "one_time_event" and record.start_date and record.start_date < now:
                    # Within the catch-up window (missed during a deploy/restart): keep it so
                    # _load_jobs re-arms it and it fires late. schedule_event gives one-time events
                    # a misfire grace covering this window, and _handle_trigger stamps it 'overdue'.
                    logger.info(
                        "Catching up one-time event %s (overdue %.0fs, within grace).",
                        record.event_id, (now - record.start_date).total_seconds(),
                    )

                # Convert once while session is open
                pyd_event = record.to_pydantic()
                self.time_events[pyd_event.event_id] = pyd_event

            if expired_ids:
                session.query(TimeEvent).filter(TimeEvent.event_id.in_(expired_ids)).delete(
                    synchronize_session=False
                )
                session.commit()
                logger.info(
                    "Deleted %d expired one-time event(s) past the catch-up window.", len(expired_ids),
                )

            logger.info(f"Loaded {len(self.time_events)} events into memory.")

        except Exception as e:
            session.rollback()
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
