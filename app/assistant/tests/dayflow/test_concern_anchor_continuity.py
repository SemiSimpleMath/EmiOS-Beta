"""A calendar event line carries its anchor: the identity of the event instance, `calendar:<google event id>`.

The brain's prompts label calendar lines from these anchors (brain_step._calendar_lines), and the calendar sense keys
events by id. Until 2026-10-09 the noticer also copied anchors into the concerns it raised, and a declined or chronic
anchor refused a re-mint; the noticer no longer raises concerns, so that suppression is gone.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.subconscious.context_builder import (
    _anchor_for_calendar_event,
    _with_calendar_anchors,
)


# --------------------------------------------------------------------------- #
# The anchor has to be an id the model was SHOWN, not a phrase it wrote
# --------------------------------------------------------------------------- #

def test_anchor_is_the_per_instance_event_id():
    """Per-instance, so this year's event and next year's are different anchors.

    That is what makes a decline expire by construction instead of needing a rule.
    """
    assert _anchor_for_calendar_event({"id": "evt_a_20260918T150000Z"}) == \
        "calendar:evt_a_20260918T150000Z"
    assert _anchor_for_calendar_event({"id": "evt_a_20270918T150000Z"}) != \
        _anchor_for_calendar_event({"id": "evt_a_20260918T150000Z"})


def test_calendar_lines_are_prefixed_with_their_anchor():
    content = (
        "Found 2 calendar events from A to B.\n"
        "- Makeup Picture Day @ 2026-09-18T15:00:00+00:00  location=South Lake\n"
        "- Dentist @ 2026-09-19T17:00:00+00:00"
    )
    events = [
        {"id": "evt_pictureday_20260918T150000Z", "summary": "Makeup Picture Day"},
        {"id": "evt_dentist", "summary": "Dentist"},
    ]
    out = _with_calendar_anchors(content, events)
    assert "- [calendar:evt_pictureday_20260918T150000Z] Makeup Picture Day @" in out
    assert "- [calendar:evt_dentist] Dentist @" in out
    # The header is not an event line and must be left alone.
    assert out.split("\n")[0] == "Found 2 calendar events from A to B."
    # Everything the tool put on the line survives — location et al are why this rewrites
    # the tool's output instead of rendering its own.
    assert "location=South Lake" in out


def test_an_event_line_with_no_start_still_gets_its_anchor():
    out = _with_calendar_anchors("- All Day Thing", [{"id": "e1", "summary": "All Day Thing"}])
    assert out == "- [calendar:e1] All Day Thing"


def test_a_line_with_no_matching_event_is_left_untouched():
    """No anchor is correct here. A guessed one would be worse than none."""
    out = _with_calendar_anchors("- Something Else @ x", [{"id": "e1", "summary": "Other"}])
    assert out == "- Something Else @ x"


def test_events_with_no_id_contribute_no_anchor():
    out = _with_calendar_anchors("- Thing @ x", [{"id": "", "summary": "Thing"}])
    assert out == "- Thing @ x"
