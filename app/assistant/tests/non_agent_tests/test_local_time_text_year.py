"""A prompt time in another year carries its year (2026-11-15).

A concern mattering until October 2027 read "Fri 10-15 07:54 AM" in every brain prompt, and its planner judged
"its stated relevance period has passed". Pins: this year's times keep the short form; another year's show it.
"""
from datetime import datetime, timedelta, timezone

from app.assistant.utils.time_utils import get_local_timezone, local_time_text


def test_this_year_keeps_the_short_form():
    now = datetime.now(timezone.utc)
    text = local_time_text(now.isoformat())
    assert str(now.astimezone(get_local_timezone()).year) not in text


def test_another_year_carries_its_year():
    later = datetime.now(timezone.utc) + timedelta(days=400)
    year = later.astimezone(get_local_timezone()).year
    assert f" {year}-" in local_time_text(later.isoformat())
    assert f" {year - 2}-" in local_time_text((later - timedelta(days=731)).isoformat(), seconds=True)


def test_blank_stays_blank():
    assert local_time_text(None) == "" and local_time_text("") == ""
