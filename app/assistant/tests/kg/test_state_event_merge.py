"""Tests for State/Event candidate filtering during proposal promotion.

These tests cover the deterministic candidate-pruning stages of
``proposal_promoter._prepare_proposal_plan``:

  Stage 1 — participant-overlap (Jaccard) scoring
  Stage 2 — Event-only date-tolerance filter

The LLM merge call (``_call_node_merger_for_state_match``) is the third
stage; it gets a separate test that mocks the agent boundary.

Why this file exists
--------------------
On 2026-05-03 we discovered that the ``Performance`` Event hub
(``0b4416dd-...``) had absorbed at least three distinct productions:

  - Annika's Beetlejuice performance at South Lake Middle School (Dec 2025)
  - A "musical theater" performance attended by Jorma + Seija (Apr 2026)
  - The Drowsy Chaperone production (May 2026, March 21 dated)

The today-merge happened *after* the merge-tightening commit
(``958ebb08``) was supposed to block over-merges across States/Events.
The smoking gun: when the candidate's ``start_date`` is None, the
date-tolerance filter currently keeps it. So a generic dateless catch-all
hub becomes a magnet for any new dated proposal whose participants
overlap. The xfail test ``test_dateless_candidate_should_be_rejected_against_dated_proposal``
documents this gap; once the fix lands, the xfail comes off.

Test layering
-------------
- Pure unit tests on the extracted helpers (no DB, no LLM) — fastest
- A handful of fixture-based integration tests that exercise the full
  ``_prepare_proposal_plan`` against an in-memory SQLite (TBD; not in
  this initial cut so we can ship the safety net quickly)

Run with::

    .venv\\Scripts\\python.exe -m pytest app/assistant/tests/kg/test_state_event_merge.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pytest

from app.assistant.kg.proposal_promoter import (
    _EVENT_DATE_TOLERANCE_DAYS,
    _filter_event_candidates_by_date,
    _score_candidates_by_participant_overlap,
)


# ---------------------------------------------------------------------------
# Lightweight stubs so tests don't need a SQLAlchemy session or real Node
# instances. The helpers under test only read attributes (`id`, `start_date`).
# ---------------------------------------------------------------------------

@dataclass
class _StubNode:
    id: str
    label: str = ""
    start_date: Optional[datetime] = None


def _ts(year: int, month: int, day: int) -> datetime:
    """UTC midnight datetime — the helpers compare via ``.date()`` so the
    time component is irrelevant."""
    return datetime(year, month, day, tzinfo=timezone.utc)


# =============================================================================
# Stage 1: participant-overlap (Jaccard) scoring
# =============================================================================

class TestParticipantOverlapScoring:
    """``_score_candidates_by_participant_overlap`` should:
    - drop candidates with no participant overlap
    - drop candidates whose participant set is empty
    - rank by Jaccard desc, ties broken by absolute overlap count
    """

    def test_zero_overlap_candidate_is_dropped(self):
        cand = _StubNode(id="cand-A")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"alice", "bob"})],
            new_participant_ids={"charlie"},
        )
        assert scored == []

    def test_empty_candidate_participants_is_dropped(self):
        cand = _StubNode(id="cand-A")
        scored = _score_candidates_by_participant_overlap(
            [(cand, set())],
            new_participant_ids={"alice"},
        )
        assert scored == []

    def test_single_overlap_kept_with_correct_jaccard(self):
        cand = _StubNode(id="cand-A")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"alice", "bob"})],
            new_participant_ids={"alice"},
        )
        assert len(scored) == 1
        assert scored[0]["node"] is cand
        assert scored[0]["overlap"] == 1
        # |{alice}| / |{alice, bob}| = 1/2
        assert scored[0]["jaccard"] == pytest.approx(0.5)

    def test_full_overlap_jaccard_is_one(self):
        cand = _StubNode(id="cand-A")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"alice", "bob"})],
            new_participant_ids={"alice", "bob"},
        )
        assert scored[0]["jaccard"] == pytest.approx(1.0)

    def test_ranking_by_jaccard_then_overlap(self):
        # A: 1/3 overlap, B: 2/2 overlap (perfect), C: 2/4 overlap
        a = _StubNode(id="a")
        b = _StubNode(id="b")
        c = _StubNode(id="c")
        scored = _score_candidates_by_participant_overlap(
            [
                (a, {"alice"}),
                (b, {"alice", "bob"}),
                (c, {"alice", "bob", "carol"}),
            ],
            new_participant_ids={"alice", "bob"},
        )
        # B perfect (1.0), C partial (2/3 ≈ 0.67), A weakest (1/2 = 0.5)
        ids = [s["node"].id for s in scored]
        assert ids == ["b", "c", "a"]


# =============================================================================
# Stage 2: Event date-tolerance filter
# =============================================================================

class TestEventDateFilter:
    """``_filter_event_candidates_by_date`` should:
    - pass everything through when the new proposal has no date
    - keep candidates within ±tolerance_days of the new proposal's date
    - drop candidates whose date is too far away
    - currently KEEP dateless candidates (the documented bug — see xfail)
    """

    def _scored(self, *cands_with_dates: tuple[str, Optional[datetime]]) -> list:
        """Build a scored-list with all jaccards = 1.0 to focus on the date filter."""
        return [
            {"node": _StubNode(id=cid, start_date=cdate), "overlap": 1, "jaccard": 1.0}
            for cid, cdate in cands_with_dates
        ]

    def test_dateless_new_proposal_passes_everything(self):
        scored = self._scored(
            ("a", _ts(2026, 1, 1)),
            ("b", _ts(2099, 12, 31)),
            ("c", None),
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=None)
        assert [s["node"].id for s in out] == ["a", "b", "c"]

    def test_within_tolerance_kept(self):
        new = _ts(2026, 5, 1)
        scored = self._scored(
            ("on", _ts(2026, 5, 1)),
            ("plus_3", _ts(2026, 5, 4)),
            ("minus_7", _ts(2026, 4, 24)),
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert {s["node"].id for s in out} == {"on", "plus_3", "minus_7"}

    def test_outside_tolerance_dropped(self):
        new = _ts(2026, 5, 1)
        scored = self._scored(
            ("plus_8", _ts(2026, 5, 9)),
            ("minus_8", _ts(2026, 4, 23)),
            ("year_off", _ts(2025, 5, 1)),
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert out == []

    def test_boundary_exactly_tolerance_kept(self):
        new = _ts(2026, 5, 1)
        scored = self._scored(
            ("plus_tol", _ts(2026, 5, 1 + _EVENT_DATE_TOLERANCE_DAYS)),
            ("minus_tol", _ts(2026, 4, 24)),  # 7 days before
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert {s["node"].id for s in out} == {"plus_tol", "minus_tol"}

    def test_dateless_candidate_currently_kept(self):
        """Documents current behavior: dateless candidate is KEPT against
        a dated new proposal. See ``test_dateless_candidate_should_be_rejected_*``
        below for the intended-future behavior.
        """
        new = _ts(2026, 5, 1)
        scored = self._scored(("dateless", None))
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert [s["node"].id for s in out] == ["dateless"]

    @pytest.mark.xfail(
        reason=(
            "KNOWN BUG (2026-05-03): a dateless candidate should be REJECTED "
            "when the new proposal has a date — generic dateless hubs are "
            "ambiguous catch-alls and should not absorb dated specifics. The "
            "Performance event 0b4416dd hit this exact path. Fix is a one-line "
            "semantic change in _filter_event_candidates_by_date."
        )
    )
    def test_dateless_candidate_should_be_rejected_against_dated_proposal(self):
        new = _ts(2026, 5, 1)
        scored = self._scored(("dateless", None))
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        # Once the fix lands: dateless candidate dropped, scored becomes empty.
        assert out == []

    def test_custom_tolerance_argument(self):
        new = _ts(2026, 5, 1)
        scored = self._scored(
            ("plus_30", _ts(2026, 5, 31)),
            ("plus_31", _ts(2026, 6, 1)),
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new, tolerance_days=30)
        assert {s["node"].id for s in out} == {"plus_30"}


# =============================================================================
# Combined-stage smoke test — what the production code actually does.
# =============================================================================

class TestCandidatePipelineComposition:
    """Make sure stage 1 + stage 2 compose the way the promoter calls them."""

    def test_high_overlap_but_far_date_is_dropped_for_events(self):
        # Beetlejuice candidate (Dec 2025), Drowsy Chaperone proposal (Mar 2026)
        # share participants but are months apart — overlap should not save
        # them from the date filter.
        beetlejuice_cand = _StubNode(id="beetlejuice", start_date=_ts(2025, 12, 6))
        drowsy_proposal_date = _ts(2026, 3, 21)

        scored = _score_candidates_by_participant_overlap(
            [(beetlejuice_cand, {"annika", "peter"})],
            new_participant_ids={"annika", "peter"},
        )
        # Stage 1: perfect overlap, kept.
        assert len(scored) == 1 and scored[0]["jaccard"] == pytest.approx(1.0)

        # Stage 2: ~105 days apart, well outside 7-day tolerance.
        out = _filter_event_candidates_by_date(scored, new_valid_from=drowsy_proposal_date)
        assert out == [], (
            "An Event with high participant overlap but a date months away "
            "must NOT survive the date filter — these are different productions."
        )

    @pytest.mark.xfail(
        reason=(
            "KNOWN BUG: when the existing Beetlejuice-era hub had its date "
            "stripped (or was never populated), the dateless bypass means "
            "high-overlap participants will glue any new dated proposal to "
            "it. This is the exact 2026-05-03 Drowsy-Chaperone-into-"
            "Performance failure mode."
        )
    )
    def test_high_overlap_dateless_hub_should_not_absorb_dated_proposal(self):
        dateless_hub = _StubNode(id="generic-performance", start_date=None)
        new_proposal_date = _ts(2026, 3, 21)

        scored = _score_candidates_by_participant_overlap(
            [(dateless_hub, {"annika", "peter"})],
            new_participant_ids={"annika", "peter"},
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new_proposal_date)
        # Once the dateless-bypass is fixed:
        assert out == []
