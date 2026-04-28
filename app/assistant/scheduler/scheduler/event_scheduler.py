
from app.assistant.ServiceLocator.service_locator import DI

from dateutil.rrule import rrule, SECONDLY
from datetime import datetime, timedelta, timezone

from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData
from app.assistant.utils.time_utils import parse_time_string, get_local_time, local_to_utc

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class EventScheduler:
    def __init__(self, event_storage, timing_engine, app):
        self.event_storage = event_storage
        self.timing_engine = timing_engine
        self.app = app  # Flask context, needed for DB operations

    def create_event(self, event):
        """
        Create and schedule an event.
        """
        try:
            logger.info(f"Creating event: {event.event_id}")

            # Schedule first (so it's live immediately)
            self.timing_engine.schedule_event(event)

            # Save to DB and memory
            with self.app.app_context():
                self.event_storage.add_time_event(event)

            logger.info(f"Event {event.event_id} created successfully.")
            return f"Event {event.event_id} created successfully."

        except Exception as e:
            logger.error("Failed to create event {event.event_id}: %s", e)
            logger.debug("failed to create event exception details", exc_info=True)
            return f"Error creating event {event.event_id}: {e}"

    def delete_event(self, event_id: str):
        """
        Delete an event from the system (storage and scheduler).
        """
        if not event_id:
            logger.error("No event_id provided to delete_event.")
            return "Error: No event_id provided."

        try:
            logger.info(f"Deleting event: {event_id}")
            with self.app.app_context():
                success = self.event_storage.remove_event(event_id, scheduler=self.timing_engine.scheduler)

            if success:
                logger.info(f"Event {event_id} deleted successfully.")
                return f"Event {event_id} deleted successfully."
            else:
                return f"Event {event_id} does not exist."

        except Exception as e:
            logger.error("Failed to delete event {event_id}: %s", e)
            logger.debug("failed to delete event exception details", exc_info=True)
            return f"Error deleting event {event_id}: {e}"


    def get_events(self, start_date=None, end_date=None):
        """
        Return a list of BaseEventData occurrences between start_date and end_date.
        All results are Pydantic BaseEventData objects.
        """
        now_local = get_local_time()
        now_utc = local_to_utc(now_local)
        one_week = timedelta(weeks=1)

        filter_start = parse_time_string(start_date) if start_date else now_utc
        filter_end = parse_time_string(end_date) if end_date else (filter_start + one_week)

        results = []

        with self.app.app_context():
            all_events = self.event_storage.get_all_time_events()

        for event in all_events:
            start = datetime.fromisoformat(event.start_date) if isinstance(event.start_date, str) else event.start_date
            end = datetime.fromisoformat(event.end_date) if isinstance(event.end_date, str) else event.end_date
            
            # Ensure timezone-aware datetimes (assume UTC if naive)
            if start and start.tzinfo is None:
                logger.warning(f"Event {event.event_id} has naive 'start' datetime, assuming UTC")
                start = start.replace(tzinfo=timezone.utc)
            if end and end.tzinfo is None:
                logger.warning(f"Event {event.event_id} has naive 'end' datetime, assuming UTC")
                end = end.replace(tzinfo=timezone.utc)

            if event.event_type == "interval":
                if not start:
                    continue

                # Determine dtstart for rrule
                if filter_start > start:
                    elapsed = (filter_start - start).total_seconds()
                    n_intervals = int(elapsed // event.interval)
                    dtstart = start + timedelta(seconds=n_intervals * event.interval)
                else:
                    dtstart = start

                recurrence_until = min(filter_end, end) if end else filter_end

                for occ in rrule(freq=SECONDLY, interval=event.interval, dtstart=dtstart, until=recurrence_until):
                    results.append(BaseEventData(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        interval=event.interval,
                        start_date=start.isoformat() if start else None,
                        end_date=end.isoformat() if end else None,
                        event_payload={
                            **(event.event_payload or {}),
                            "occurrence": occ.isoformat(),
                        }
                    ))

            elif event.event_type == "one_time_event":
                if start and filter_start <= start <= filter_end:
                    results.append(BaseEventData(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        interval=event.interval,
                        start_date=start.isoformat() if start else None,
                        end_date=end.isoformat() if end else None,
                        event_payload=event.event_payload or {},
                    ))

        return results
