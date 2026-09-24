"""A decline outlives the concern_id it was given about — anchored concern identity.

THE GAP THIS CLOSES. A concern's id is the identity of the NOTICING; it is minted fresh every
tick the noticer notices something. `apply_noticer_output` deduped on concern_id against the
active and addressing buckets only. So declining a concern parked it dormant, and on the next
tick the evidence that produced it was STILL in context (the calendar event has not moved), the
same worry was re-derived, and a brand-new UUID never collided with the dormant original.

Observed: a child's makeup picture day was declined on 2026-09-17 with the owner's words
recorded, and a new concern for the same event was minted on 2026-09-18. Five concerns existed
for that one event. A school evening event produced three concerns in two days, one of them
carrying a decline. `context_builder._build_concerns_recently_closed` cites a sleep concern
re-minted five times, twice past an accept_chronic that had deliberately archived it.

THE FIX. A concern may carry an ``anchor``: the identity of the THING it is about, copied from
the context (``calendar:<google event id>``), never paraphrased from a title. A standing ruling
— an owner decline, or accept_chronic — is stored on the anchored concern, and a new concern
whose anchor is already settled is refused and journalled against the original.

WHAT IS DELIBERATELY NOT SUPPRESSED, because getting this wrong breaks the feature to fix the
bug: a `resolved` anchor. Resolved means the need was met or the moment passed, which is no
reason to refuse the next one. Monthly timesheets and the dogs' flea medication legitimately
mint again each cycle.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/dayflow/test_concern_anchor_continuity.py
"""
from __future__ import annotations

import json
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.subconscious.context_builder import (
    _anchor_for_calendar_event,
    _with_calendar_anchors,
)
from app.assistant.subconscious.persist import (
    _settled_anchors,
    apply_noticer_output,
)

_PICTURE_DAY = "calendar:evt_pictureday_20260918T150000Z"
_DECLINED_CID = "3a9f2c71-6b84-4d15-ae32-91f0c7b5e628"


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


# --------------------------------------------------------------------------- #
# Which rulings are standing, and which are not
# --------------------------------------------------------------------------- #

def test_a_decline_and_a_chronic_acceptance_are_standing_rulings():
    register = {
        "active": [], "addressing": [],
        "dormant": [
            {"concern_id": "c1", "anchor": "calendar:declined",
             "user_declined_at_utc": "2026-09-17T21:31:20+00:00"},
            {"concern_id": "c2", "anchor": "calendar:chronic", "chronic": True,
             "dormant_reason": "long-term pattern"},
        ],
        "resolved": [],
    }
    settled = _settled_anchors(register)
    assert set(settled) == {"calendar:declined", "calendar:chronic"}


def test_resolved_and_plain_dormant_are_NOT_standing_rulings():
    """The guard that must not over-reach.

    Resolved means met-or-passed, not refused. Recurring obligations (timesheets, pet
    medication) mint per cycle and must keep doing so.
    """
    register = {
        "active": [], "addressing": [],
        "resolved": [{"concern_id": "r1", "anchor": "calendar:timesheets",
                      "resolved_at_utc": "2026-09-01T00:00:00+00:00",
                      "resolution_reason": "the deadline passed"}],
        "dormant": [{"concern_id": "d1", "anchor": "calendar:parked",
                     "dormant_at_utc": "2026-09-01T00:00:00+00:00"}],
    }
    assert _settled_anchors(register) == {}


def test_concerns_without_an_anchor_never_settle_anything():
    register = {
        "active": [], "addressing": [], "resolved": [],
        "dormant": [{"concern_id": "c1", "user_declined_at_utc": "2026-09-17T00:00:00+00:00"}],
    }
    assert _settled_anchors(register) == {}


# --------------------------------------------------------------------------- #
# The re-mint itself
# --------------------------------------------------------------------------- #

def _register_with_declined_picture_day(tmp_path: Path) -> Path:
    path = tmp_path / "register.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "active": [], "addressing": [], "resolved": [],
        "dormant": [{
            "concern_id": _DECLINED_CID,
            "anchor": _PICTURE_DAY,
            "title": "South Lake makeup Picture Day needs clothing and packet preparation",
            "user_declined_at_utc": "2026-09-17T21:31:20+00:00",
            "reinforcement_notes": '\n[2026-09-17T21:31:20+00:00] USER DECLINED via work_x: "already done"',
        }],
    }), encoding="utf-8")
    return path


def _new_concern(anchor, *, cid="new-1", title="Picture Day is today and prep needs confirming"):
    concern = {
        "concern_id": cid, "title": title, "kind": "anticipated_need",
        "severity": "medium", "horizon": "today", "domain_tags": ["family"],
        "addressable_by": ["dayflow_orchestrator"], "evidence": [],
        "notes": "n", "first_observed": "2026-09-18T14:13:00-07:00",
    }
    if anchor is not None:
        concern["anchor"] = anchor
    return concern


def _apply(path, concerns):
    return apply_noticer_output(
        {"new_concerns": concerns},
        register_path=path,
        tick_log_path=path.parent / "tick.jsonl",
    )


def test_the_real_case_a_declined_event_is_not_re_minted(tmp_path):
    """09-17 declined, 09-18 the same event came back under a new id. Not any more."""
    path = _register_with_declined_picture_day(tmp_path)
    _apply(path, [_new_concern(_PICTURE_DAY)])

    reg = json.loads(path.read_text(encoding="utf-8"))
    assert reg["active"] == [], "a settled anchor must not reappear as a live concern"

    prior = reg["dormant"][0]
    assert prior["concern_id"] == _DECLINED_CID, "the original must stay put"
    assert prior["suppressed_remint_count"] == 1
    assert prior["suppressed_remint_at_utc"]
    # Auditable, not silent: the attempt is journalled on the concern that ruled.
    assert "SUPPRESSED a re-mint" in prior["reinforcement_notes"]
    assert "declined" in prior["reinforcement_notes"]
    # And the owner's original words are still there.
    assert "USER DECLINED" in prior["reinforcement_notes"]


def test_repeated_attempts_are_counted_not_lost(tmp_path):
    path = _register_with_declined_picture_day(tmp_path)
    _apply(path, [_new_concern(_PICTURE_DAY, cid="n1")])
    _apply(path, [_new_concern(_PICTURE_DAY, cid="n2")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert reg["active"] == []
    assert reg["dormant"][0]["suppressed_remint_count"] == 2


def test_a_different_event_is_unaffected(tmp_path):
    """The guard is per-thing. Declining one event must not mute the household."""
    path = _register_with_declined_picture_day(tmp_path)
    _apply(path, [_new_concern("calendar:evt_dentist", cid="n2", title="Dentist needs confirming")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert [c["concern_id"] for c in reg["active"]] == ["n2"]


def test_an_unanchored_concern_is_still_admitted(tmp_path):
    """Patterns anchor to nothing ("sleep has been poor") and must keep flowing."""
    path = _register_with_declined_picture_day(tmp_path)
    _apply(path, [_new_concern(None, cid="n3", title="Sleep has been poor again")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert [c["concern_id"] for c in reg["active"]] == ["n3"]


def test_a_resolved_anchor_may_be_re_minted(tmp_path):
    """Recurring obligations depend on this. Resolved is not a refusal."""
    path = tmp_path / "register.json"
    path.write_text(json.dumps({
        "schema_version": 1, "active": [], "addressing": [], "dormant": [],
        "resolved": [{"concern_id": "old", "anchor": "calendar:timesheets",
                      "resolved_at_utc": "2026-08-31T00:00:00+00:00"}],
    }), encoding="utf-8")
    _apply(path, [_new_concern("calendar:timesheets", cid="n4", title="Timesheets due again")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert [c["concern_id"] for c in reg["active"]] == ["n4"]


def test_a_chronic_acceptance_also_holds(tmp_path):
    """The docstring's sleep concern: re-minted twice past an accept_chronic."""
    path = tmp_path / "register.json"
    path.write_text(json.dumps({
        "schema_version": 1, "active": [], "addressing": [], "resolved": [],
        "dormant": [{"concern_id": "old", "anchor": "calendar:club_drive", "chronic": True,
                     "dormant_reason": "standing pattern, not worth ticking"}],
    }), encoding="utf-8")
    _apply(path, [_new_concern("calendar:club_drive", cid="n5")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert reg["active"] == []
    assert "accepted as chronic" in reg["dormant"][0]["reinforcement_notes"]


def test_existing_unanchored_history_is_unaffected(tmp_path):
    """Every concern already in the register predates anchors. Nothing may break on them."""
    path = tmp_path / "register.json"
    path.write_text(json.dumps({
        "schema_version": 1, "active": [], "addressing": [], "resolved": [],
        "dormant": [{"concern_id": "legacy", "title": "no anchor field at all",
                     "user_declined_at_utc": "2026-09-01T00:00:00+00:00"}],
    }), encoding="utf-8")
    _apply(path, [_new_concern(_PICTURE_DAY, cid="n6")])
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert [c["concern_id"] for c in reg["active"]] == ["n6"], (
        "an unanchored legacy decline cannot suppress anything, and must not crash"
    )
