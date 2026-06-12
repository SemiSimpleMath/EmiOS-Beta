"""Temporal rendering for the context enricher's KG context.

A date-less Event line is how a road trip planned weeks out got asserted
as currently happening (and a 'home in Marysville' fabricated on top).
_node_dates_suffix must state the temporal facts deterministically so the
enricher LLM can never confuse planned-future with ongoing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.assistant.control_nodes.context_enricher_prep_node import _node_dates_suffix


def _node(node_type="Event", start=None, end=None, start_conf=None, end_conf=None):
    return SimpleNamespace(
        node_type=node_type,
        start_date=start,
        end_date=end,
        start_date_confidence=start_conf,
        end_date_confidence=end_conf,
    )


NOW = datetime.now(timezone.utc)


def test_future_event_is_marked_upcoming():
    suffix = _node_dates_suffix(_node(
        start=NOW + timedelta(days=18), end=NOW + timedelta(days=21),
    ))
    assert "UPCOMING" in suffix
    assert "has not happened yet" in suffix
    assert (NOW + timedelta(days=18)).strftime("%Y-%m-%d") in suffix


def test_past_event_is_marked_past():
    suffix = _node_dates_suffix(_node(
        start=NOW - timedelta(days=30), end=NOW - timedelta(days=27),
    ))
    assert suffix.startswith(" (past:")
    assert (NOW - timedelta(days=27)).strftime("%Y-%m-%d") in suffix


def test_currently_running_event_is_ongoing():
    suffix = _node_dates_suffix(_node(
        start=NOW - timedelta(days=1), end=NOW + timedelta(days=2),
    ))
    assert "ongoing" in suffix


def test_state_with_open_end_renders_since():
    suffix = _node_dates_suffix(_node(
        node_type="State", start=NOW - timedelta(days=400),
    ))
    assert suffix.startswith(" (since ")


def test_entity_with_start_renders_since_not_dated():
    # A person's start_date is their birth date — "since", not "dated".
    suffix = _node_dates_suffix(_node(
        node_type="Entity", start=datetime(2010, 6, 2, tzinfo=timezone.utc),
    ))
    assert suffix == " (since 2010-06-02)"


def test_event_with_only_past_start_renders_dated():
    suffix = _node_dates_suffix(_node(start=NOW - timedelta(days=400)))
    assert suffix.startswith(" (dated ")


def test_no_dates_renders_nothing():
    assert _node_dates_suffix(_node()) == ""


def test_year_floor_dates_render_at_honest_precision():
    # Jan-1 + estimated = year-only knowledge → "2003", never "2003-01-01".
    suffix = _node_dates_suffix(_node(
        start=datetime(2003, 1, 1, tzinfo=timezone.utc), start_conf="estimated",
    ))
    assert "(dated 2003)" in suffix
    assert "2003-01" not in suffix


def test_naive_datetimes_are_treated_as_utc():
    # SQLite can hand back naive datetimes; comparison must not raise.
    suffix = _node_dates_suffix(_node(
        start=(NOW + timedelta(days=10)).replace(tzinfo=None),
    ))
    assert "UPCOMING" in suffix
