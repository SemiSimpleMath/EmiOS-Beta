"""A daily or weekly reminder keeps its wall-clock time across a daylight-saving change.

2026-09-12: Friday Night Meats had five weekly reminders anchored at 6:00, 7:00, 7:30,
7:50 and 8:00 PM PST for an event starting at 8 PM. Under PDT every one of them fired an
hour late, so the whole ladder landed after the event had begun. A second reminder ladder
for a weekly video call had drifted identically. Both were built as APScheduler interval
triggers, which fire at a fixed absolute spacing from a UTC anchor and therefore cannot
hold a wall-clock time.

An interval that is a whole day or week means "this time every day / every week", so it is
now built as a cron trigger pinned to the local zone. Genuinely periodic intervals -- a
five-minute poll, an hourly monitor -- are untouched, because DST does not apply to them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.assistant.scheduler.scheduler.timing_engine import TimingEngine

LA = ZoneInfo("America/Los_Angeles")
DAY = 86400
WEEK = 604800


class _Engine:
    """TimingEngine's trigger builder without starting APScheduler or Flask."""
    _SECONDS_PER_DAY = TimingEngine._SECONDS_PER_DAY
    _wall_clock_cron = TimingEngine._wall_clock_cron

    class _Log:
        def error(self, *a, **k):
            raise AssertionError("cron build failed: %r" % (a,))

    logger = _Log()


def _cron(interval, local_wall):
    """Build the trigger from an anchor expressed as a LOCAL wall time."""
    return _Engine()._wall_clock_cron(interval, local_wall.astimezone(timezone.utc))


def _next_fire_local(trigger, after_local):
    nxt = trigger.get_next_fire_time(None, after_local.astimezone(timezone.utc))
    return nxt.astimezone(LA)


# The real Friday Night Meats ladder, authored under PST for an 8 PM event.
LADDER_PST = [(18, 0), (19, 0), (19, 30), (19, 50), (20, 0)]


@pytest.mark.parametrize("hour,minute", LADDER_PST)
def test_a_weekly_reminder_holds_its_wall_clock_time_across_dst(hour, minute):
    anchor = datetime(2025, 11, 21, hour, minute, tzinfo=LA)   # a Friday, PST
    trigger = _cron(WEEK, anchor)
    assert trigger is not None, "a weekly interval must become a wall-clock trigger"

    in_pst = _next_fire_local(trigger, datetime(2026, 1, 5, tzinfo=LA))
    in_pdt = _next_fire_local(trigger, datetime(2026, 7, 6, tzinfo=LA))

    assert (in_pst.hour, in_pst.minute) == (hour, minute)
    assert (in_pdt.hour, in_pdt.minute) == (hour, minute), (
        "the whole point: the wall time must not slide when the clocks change")
    assert in_pst.weekday() == in_pdt.weekday() == anchor.weekday()


def test_a_daily_reminder_holds_its_wall_clock_time_across_dst():
    anchor = datetime(2025, 8, 11, 7, 50, tzinfo=LA)           # "take out the dogs"
    trigger = _cron(DAY, anchor)
    assert trigger is not None
    for probe in (datetime(2026, 1, 5, tzinfo=LA), datetime(2026, 7, 6, tzinfo=LA)):
        fire = _next_fire_local(trigger, probe)
        assert (fire.hour, fire.minute) == (7, 50)


def test_the_old_interval_trigger_is_what_drifted():
    """Guard the diagnosis itself, so nobody 'simplifies' back to an interval trigger."""
    from apscheduler.triggers.interval import IntervalTrigger

    anchor = datetime(2025, 11, 21, 20, 0, tzinfo=LA).astimezone(timezone.utc)
    old = IntervalTrigger(seconds=WEEK, start_date=anchor, timezone=timezone.utc)
    pst = old.get_next_fire_time(None, datetime(2026, 1, 5, tzinfo=timezone.utc)).astimezone(LA)
    pdt = old.get_next_fire_time(None, datetime(2026, 7, 6, tzinfo=timezone.utc)).astimezone(LA)
    assert pst.hour == 20
    assert pdt.hour == 21, "an interval trigger slides an hour into PDT — the bug"


@pytest.mark.parametrize("interval", [300, 3600, 10800, 3 * DAY])
def test_intervals_that_are_not_a_day_or_a_week_stay_periodic(interval):
    anchor = datetime(2026, 4, 1, 22, 33, tzinfo=LA)
    assert _cron(interval, anchor) is None


def test_a_missing_interval_stays_periodic():
    anchor = datetime(2026, 4, 1, 22, 33, tzinfo=LA)
    assert _cron(0, anchor) is None
    assert _cron(None, anchor) is None


# ── a wall-clock reminder keeps its start and end (2026-10-07) ──────────────
# APScheduler ignores add_job's trigger arguments when handed a trigger object, and the cron
# trigger was built without start or end: South Lake's registration reminder (ended 2025-08-13),
# omeprazole (ended 2025-08-21) and two more fired every day for months.

def test_an_ended_daily_reminder_never_fires_again():
    anchor = datetime(2025, 8, 11, 8, 0, tzinfo=LA).astimezone(timezone.utc)
    end = datetime(2025, 8, 13, 8, 0, tzinfo=LA).astimezone(timezone.utc)
    trigger = _Engine()._wall_clock_cron(DAY, anchor, end)
    assert trigger.get_next_fire_time(None, datetime(2025, 8, 12, tzinfo=LA)).astimezone(LA).day == 12
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 7, tzinfo=LA)) is None


def test_a_weekly_reminder_does_not_fire_before_its_start():
    anchor = datetime(2026, 11, 6, 18, 0, tzinfo=LA)          # a Friday in the future
    trigger = _cron(WEEK, anchor)
    first = _next_fire_local(trigger, datetime(2026, 10, 7, tzinfo=LA))
    assert first == anchor


def test_scheduling_an_ended_daily_event_arms_a_job_that_never_fires():
    from types import SimpleNamespace
    added = {}

    class Engine(_Engine):
        scheduler = SimpleNamespace(add_job=lambda **kw: added.update(kw))
        _handle_trigger = staticmethod(lambda event_id: None)

        class _Log(_Engine._Log):
            def info(self, *a, **k): pass
            def debug(self, *a, **k): pass
        logger = _Log()

    event = SimpleNamespace(event_id="south-lake", event_type="interval", interval=DAY, jitter=None,
                            start_date="2025-08-11T15:00:00+00:00", end_date="2025-08-13T15:00:00+00:00")
    TimingEngine.schedule_event(Engine(), event)
    assert added["id"] == "south-lake" and "end_date" not in added
    assert added["trigger"].get_next_fire_time(None, datetime(2026, 10, 7, tzinfo=timezone.utc)) is None
