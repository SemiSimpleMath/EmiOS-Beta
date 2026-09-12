from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone

from app.assistant.scheduler.storage.event_storage import ONE_TIME_CATCHUP_GRACE_SECONDS
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class TimingEngine:
    def __init__(self, event_storage, event_executor, app):
        """
        Initializes the timing engine.

        Args:
            event_storage: EventStorage instance to retrieve events.
            event_executor: EventExecutor instance to handle event execution.
            app: Flask app context for DB operations.
        """
        self.scheduler = BackgroundScheduler(timezone=timezone.utc)
        self.event_storage = event_storage
        self.executor = event_executor
        self.app = app
        self.logger = logger

        self.start()
        self._load_jobs()
        self.logger.info("TimingEngine initialized.")

    def start(self):
        if not self.scheduler.running:
            self.logger.info("Starting APScheduler...")
            self.scheduler.start()

    def shutdown(self):
        self.logger.info("Shutting down APScheduler...")
        self.scheduler.shutdown()

    def _load_jobs(self):
        self.logger.info("Reconstructing jobs from EventStorage...")
        all_events = self.event_storage.get_all_time_events()
        self.logger.info(f"Found {len(all_events)} events to restore.")
        for event in all_events:
            try:
                self.schedule_event(event)
            except Exception as e:
                self.logger.error(f"Failed to schedule event {event.event_id}: {e}")

    # A one-time event firing more than this after its scheduled time is "late" — it was caught
    # up on boot after a downtime that spanned it, not fired on schedule. Stamp it so the handler
    # doesn't present it as on-time. A comfortable margin over normal fire jitter.
    _OVERDUE_MARK_THRESHOLD_SECONDS = 60

    def _handle_trigger(self, event_id: str):
        """
        Called by APScheduler when a job fires. Loads the full event and executes it.

        A one-time event is stamped 'overdue' when it fires meaningfully later than its scheduled
        time (i.e. it was caught up after a downtime), and its durable row is deleted after it
        fires — without that, one-time events accumulate in time_events forever (load_events only
        skips past ones, never deletes them, so the table and the boot scan grow without bound).
        """
        try:
            self.logger.debug(f"Handling trigger for event_id: {event_id}")
            event = self.event_storage.get_time_event(event_id)
            if not event:
                self.logger.error(f"No event found for ID: {event_id}")
                return

            is_one_time = getattr(event, "event_type", None) == "one_time_event"
            if is_one_time:
                self._mark_overdue_if_late(event)

            with self.app.app_context():
                self.executor.execute(event)

            if is_one_time:
                # Delete-on-fire: a one-time event has done its job. Interval events recur, so
                # they are never deleted here.
                self.event_storage.remove_event(event_id)

        except Exception as e:
            self.logger.error("Error handling trigger for event_id {event_id}: %s", e)
            self.logger.debug("error handling trigger for event_id exception details", exc_info=True)

    def _mark_overdue_if_late(self, event) -> None:
        """Stamp a one-time event 'overdue' if it's firing well after its scheduled time (caught
        up after a downtime). No-op for an on-time fire, so the marker means exactly 'delayed'."""
        start = event.start_date
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start)
            except Exception as e:
                # non-fatal (the fire proceeds; only the overdue stamp is lost) but never silent
                self.logger.error(
                    "overdue check: unparseable start_date %r on event %s — skipping the stamp: %s",
                    start, getattr(event, "event_id", "?"), e)
                return
        if not start:
            return
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        overdue = (datetime.now(timezone.utc) - start).total_seconds()
        if overdue <= self._OVERDUE_MARK_THRESHOLD_SECONDS:
            return
        payload = dict(event.event_payload or {})
        payload["overdue"] = True
        payload["overdue_seconds"] = int(overdue)
        payload["scheduled_for"] = start.isoformat()
        event.event_payload = payload
        self.logger.info(
            "One-time event %s fired %.0fs late — marked overdue.", event.event_id, overdue,
        )

    # Intervals that are whole days mean "this time every day / every week" in WALL-CLOCK
    # terms. An interval trigger cannot express that: it fires at a fixed absolute spacing
    # from a UTC anchor, so every daylight-saving change slides the local time by an hour
    # and it never comes back. Measured 2026-09-12: a Friday Night Meats reminder ladder
    # authored at 6:00 / 7:00 / 7:30 / 7:50 / 8:00 PM PST for an 8 PM event was firing at
    # 7:00 through 9:00 PM PDT, so every rung landed after the thing had started. The same
    # had happened to a weekly video-call ladder. The historical response was to add a new
    # reminder at the corrected time and leave the drifted one running, which is why there
    # were five of each.
    _SECONDS_PER_DAY = 86400

    def _wall_clock_cron(self, interval_seconds, utc_anchor):
        """A cron trigger holding the anchor's LOCAL wall time, or None if not day-aligned.

        The anchor is converted with a date-aware zone, so it yields the wall time the
        event was AUTHORED at (a November anchor reads as PST) rather than that instant
        re-read under today's offset. That intent is then pinned to the local zone, and
        the tz database moves it with the clocks from here on.

        Sub-daily intervals (a 5-minute poll, an hourly monitor) are genuinely periodic:
        DST does not apply to them and they stay interval triggers.
        """
        if not interval_seconds or interval_seconds % self._SECONDS_PER_DAY:
            return None
        days = interval_seconds // self._SECONDS_PER_DAY
        if days not in (1, 7):
            # e.g. every 3 days has no plain cron expression; leave it periodic.
            return None
        try:
            from apscheduler.triggers.cron import CronTrigger

            from app.assistant.utils.time_utils import get_local_timezone
            local_tz = get_local_timezone()
            local = utc_anchor.astimezone(local_tz)
            fields = {"hour": local.hour, "minute": local.minute, "second": local.second,
                      "timezone": local_tz}
            if days == 7:
                fields["day_of_week"] = local.weekday()   # cron: 0 = Monday, as datetime
            return CronTrigger(**fields)
        except Exception as e:
            # Never lose the job over this: fall through to the interval trigger, loudly.
            self.logger.error(
                "wall-clock cron build failed for interval=%s anchor=%s: %s — keeping the "
                "drift-prone interval trigger", interval_seconds, utc_anchor, e, exc_info=True)
            return None

    def schedule_event(self, event):
        """
        Schedule a new event with APScheduler.
        """
        try:
            start_date = event.start_date
            if isinstance(start_date, str):
                start_date = datetime.fromisoformat(start_date)
            if not start_date:
                self.logger.error(f"Missing start_date for event {event.event_id}")
                return
            start_date = start_date.replace(tzinfo=timezone.utc) if start_date.tzinfo is None else start_date

            if event.event_type == "one_time_event":
                self.scheduler.add_job(
                    func=self._handle_trigger,
                    trigger="date",
                    run_date=start_date,
                    args=[event.event_id],
                    id=event.event_id,
                    # Grace covers the catch-up window so an event missed during a short downtime
                    # still fires when re-armed on boot (its run_date is then in the past).
                    # _handle_trigger stamps it 'overdue'; load_events drops anything older.
                    misfire_grace_time=ONE_TIME_CATCHUP_GRACE_SECONDS + 60,
                )
                self.logger.info(f"Scheduled one-time event {event.event_id} at {start_date}")

            elif event.event_type == "interval":
                end_date = event.end_date
                if isinstance(end_date, str):
                    end_date = datetime.fromisoformat(end_date)
                if end_date:
                    end_date = end_date.replace(tzinfo=timezone.utc) if end_date.tzinfo is None else end_date

                cron = self._wall_clock_cron(event.interval, start_date)
                if cron is not None:
                    self.scheduler.add_job(
                        func=self._handle_trigger,
                        trigger=cron,
                        args=[event.event_id],
                        id=event.event_id,
                        end_date=end_date,
                        jitter=event.jitter,
                        misfire_grace_time=300,
                    )
                    self.logger.debug(
                        "Scheduled wall-clock event %s: %s (anchor %s)",
                        event.event_id, cron, start_date,
                    )
                else:
                    self.scheduler.add_job(
                        func=self._handle_trigger,
                        trigger="interval",
                        args=[event.event_id],
                        id=event.event_id,
                        seconds=event.interval,
                        start_date=start_date,
                        end_date=end_date,
                        jitter=event.jitter,
                        misfire_grace_time=300,
                    )
                    self.logger.debug(
                        f"Scheduled interval event {event.event_id} every {event.interval} seconds starting at {start_date}"
                    )

            else:
                self.logger.error(f"Unsupported event type: {event.event_type}")

        except Exception as e:
            self.logger.error("Failed to schedule event {event.event_id}: %s", e)
            self.logger.debug("failed to schedule event exception details", exc_info=True)

    def reset(self):
        if not self.scheduler.running:
            self.scheduler.start()
        self.scheduler.remove_all_jobs()
        self.logger.info("Cleared all APScheduler jobs.")
        self.event_storage.clear_all_events()
        self.logger.info("Cleared all stored events.")
