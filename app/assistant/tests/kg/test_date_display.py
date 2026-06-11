"""format_node_date — precision-honest rendering of floored KG dates.

Year/month-only knowledge is stored floored (YYYY-01-01 / YYYY-MM-01,
confidence estimated/inferred) purely as a chronology sort key; renderers
must never present the fabricated day as known (user directive
2026-06-10, the '2003-01-01 wedding' incident)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.kg_core.kg_utils.date_display import format_node_date, sort_key_ts


def test_year_floor_renders_year_only():
    assert format_node_date(datetime(2003, 1, 1), "estimated") == "2003"
    assert format_node_date(datetime(2003, 1, 1), "inferred") == "2003"


def test_month_floor_renders_year_month():
    assert format_node_date(datetime(1994, 8, 1), "estimated") == "1994-08"


def test_authoritative_dates_render_fully_even_on_floor_days():
    # A real New Year's Day / first-of-month with authoritative confidence
    # is a genuine day, not a floor.
    assert format_node_date(datetime(2004, 1, 1), "actual") == "2004-01-01"
    assert format_node_date(datetime(2004, 1, 1), "user_set") == "2004-01-01"
    assert format_node_date(datetime(1994, 8, 1), "explicit") == "1994-08-01"


def test_non_floor_estimated_dates_render_fully():
    # estimated but mid-month/mid-year: a concrete (if uncertain) day.
    assert format_node_date(datetime(2003, 9, 3), "estimated") == "2003-09-03"


def test_no_confidence_renders_fully():
    assert format_node_date(datetime(2003, 1, 1), None) == "2003-01-01"


def test_none_and_tz():
    assert format_node_date(None, "estimated") is None
    aware = datetime(2003, 1, 1, tzinfo=timezone.utc)
    assert format_node_date(aware, "estimated") == "2003"


def test_sort_key_ts_handles_mixed_tz_and_none():
    naive = datetime(2003, 1, 1)
    aware = datetime(2003, 1, 1, tzinfo=timezone.utc)
    assert sort_key_ts(naive) == sort_key_ts(aware)
    assert sort_key_ts(None) == 0.0
