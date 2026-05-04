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
    end_date: Optional[datetime] = None
    start_date_prose: str = ""  # "in 2023", "last fall" — partial info
    end_date_prose: str = ""


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

    def test_dateless_candidate_kept_against_dated_proposal(self):
        """A dateless candidate is KEPT for the LLM to evaluate when the
        new proposal has a date. This is INTENTIONAL: most State/Event
        nodes start dateless and get dates filled in over time. A vague
        dateless mention being refined later by a dated one is a normal
        graph-evolution flow; both should resolve to the same node.

        The 2026-05-03 Performance over-merge problem is NOT a date-filter
        bug — it's caught by participant-overlap-strength filters (Jaccard
        threshold / hub weighting) instead.
        """
        new = _ts(2026, 5, 1)
        scored = self._scored(("dateless", None))
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert [s["node"].id for s in out] == ["dateless"]

    # NOTE: an earlier xfail here proposed REJECTING dateless candidates
    # against dated proposals as the fix for the 2026-05-03 Performance
    # over-merge. That hypothesis was retracted: most State/Event nodes
    # start dateless and get dates filled in over time; rejecting them
    # would break the legitimate "vague mention later refined by dated
    # mention" merge flow. The Performance over-merge is instead caught
    # at the participant-overlap-strength layer (Jaccard threshold /
    # hub-weighted overlap). See test_actual_performance_case_via_jaccard_threshold.

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

    def test_high_overlap_dateless_hub_kept_for_llm(self):
        """A dateless candidate with strong participant overlap is KEPT
        for LLM evaluation. The candidate may legitimately be a vague
        mention of the same event the new proposal is now refining with
        a date. The LLM (or Jaccard-threshold layer) decides; the date
        filter alone is not evidence of non-match."""
        dateless_hub = _StubNode(id="generic-performance", start_date=None)
        new_proposal_date = _ts(2026, 3, 21)

        scored = _score_candidates_by_participant_overlap(
            [(dateless_hub, {"annika", "peter"})],
            new_participant_ids={"annika", "peter"},
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new_proposal_date)
        assert len(out) == 1


# =============================================================================
# Desired decision tree (Jukka 2026-05-03)
# =============================================================================
# The MINIMUM merge decision tree for State/Event/Goal nodes:
#
#   Hard rejects (no LLM, no merge):
#     1. Different node_type  → NOT same
#     2. Both time-frames KNOWN and they don't match  → NOT same
#     3. Different participants (zero overlap)  → NOT same
#
#   LLM invocation conditions (only when):
#     4. All hard-reject checks pass AND label/title is the same
#        → invoke LLM with as much context as possible about both nodes
#
# Otherwise: don't merge.
#
# Some of these are already enforced by the current pipeline (type filter
# is implicit in the candidate query; zero-overlap drop happens in the
# scorer; the Event date filter exists). Others are gaps:
#
#   - States have NO time-frame check at all (only Events do). Per the
#     spec, identity States like "Marriage" persist across time so a date
#     mismatch isn't necessarily a different state — but for *episodic*
#     States ("Rehearsing for X", "Working on project Y") with end dates,
#     known-and-different dates SHOULD drop. Currently they don't.
#   - There is NO label-equality precheck before invoking the LLM. The
#     LLM is called for any candidate that passes overlap + date filter.
#     Per the spec, the LLM should only see candidates whose label
#     matches (exact, case-insensitive); different-label candidates are
#     dropped without LLM cost.
#   - The LLM merger may not receive full context today — needs a separate
#     audit of _call_node_merger_for_state_match's payload.
#
# Tests below xfail until the spec lands. The test names describe the
# desired behavior; none of these change current production behavior.
# =============================================================================

# ---------- Helper functions the spec implies (NOT YET IMPLEMENTED) ----------
# These would live in proposal_promoter.py once the spec is approved. The
# tests reference them via the same module namespace so the import will
# fail (and the xfail will then catch the import error → still xfailing).
# When implemented for real, the imports succeed and the assertions run.

def _labels_match_for_merge(a: str, b: str) -> bool:
    """Stub of the future label-equality check. Case-insensitive trim
    equality. Returns True iff labels are 'same enough' to pass to LLM.

    Currently unused by production code — the spec wants this as a gate
    BEFORE _call_node_merger_for_state_match. Kept here so the tests
    below have something to call; the eventual implementation should
    live in proposal_promoter.py.
    """
    return (a or "").strip().casefold() == (b or "").strip().casefold()


class TestDesiredDecisionTree:
    """Encodes the spec. Many of these will xfail today; flipping to PASS
    is the litmus test for the merge-tightening work."""

    # ---- Hard reject 1: different node_type ----

    def test_different_node_type_should_not_be_candidates(self):
        """Already enforced by the candidate query (Node.node_type ==
        pn.node_type). This test pins the behavior at the helper level
        so a future refactor doesn't accidentally relax it. We assert
        via the documented invariant: callers MUST pre-filter by type
        before invoking _score_candidates_by_participant_overlap.

        No production behavior to assert here — this is a contract test
        for callers. The body is intentionally just a documentation no-op
        until we have an integration-level test against
        _prepare_proposal_plan.
        """
        # Documented: candidate_lists are populated only with nodes where
        # Node.node_type == pn.node_type (proposal_promoter.py around line
        # 1006). If a future change drops that filter, this comment is
        # the last line of defense — the helpers below assume it.
        assert True

    # ---- Hard reject 2: both time-frames KNOWN and they don't match ----

    def test_event_both_dates_known_and_far_apart_drops(self):
        """Already passing — see TestEventDateFilter::test_outside_tolerance_dropped.
        Re-asserted here in the spec section so the decision tree is
        complete in one place."""
        new = _ts(2026, 5, 1)
        scored = [
            {"node": _StubNode(id="a", start_date=_ts(2026, 4, 1)),
             "overlap": 1, "jaccard": 1.0},
        ]
        out = _filter_event_candidates_by_date(scored, new_valid_from=new)
        assert out == []

    @pytest.mark.xfail(
        reason=(
            "GAP: episodic States ('Rehearsing for X' has end_date) currently "
            "have NO date filter — only Events do. Identity States "
            "(Marriage, Residence) are correctly excluded from this filter, "
            "but episodic States need it. Spec calls for: if BOTH state "
            "dates are known and they don't overlap, drop. Not implemented."
        )
    )
    def test_state_both_dates_known_and_far_apart_should_drop(self):
        """Spec: episodic States with known + non-overlapping dates are
        different instances, just like Events.

        This will require either (a) a separate _filter_state_candidates_by_date
        helper that's date-tolerance-tighter than the Event filter and
        only fires when BOTH dates are known, or (b) extending the Event
        filter to cover State and detecting identity-State labels to skip.
        """
        # When the State date filter exists, this assertion runs:
        from datetime import datetime, timezone

        # Pretend a future _filter_state_candidates_by_date exists.
        # Importing it here at runtime so the xfail catches ImportError too.
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_state_candidates_by_date,
            )
        except ImportError:
            pytest.fail("_filter_state_candidates_by_date not implemented")

        new = _ts(2026, 5, 1)
        cand = _StubNode(id="state-a", start_date=_ts(2025, 1, 1))
        cand.end_date = _ts(2025, 6, 1)
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_state_candidates_by_date(scored, new_valid_from=new)
        assert out == []

    # ---- Hard reject 3: different participants ----

    def test_zero_participant_overlap_drops(self):
        """Already passing — see TestParticipantOverlapScoring. Re-asserted
        here to complete the decision-tree picture."""
        cand = _StubNode(id="cand-A")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"alice"})],
            new_participant_ids={"bob"},
        )
        assert scored == []

    # ---- LLM invocation gate: title (label) must match ----

    def test_label_equality_check_helper_works(self):
        """The helper itself is trivial — exact case-insensitive match."""
        assert _labels_match_for_merge("Performance", "performance")
        assert _labels_match_for_merge("Performance", " Performance ")
        assert not _labels_match_for_merge("Performance", "Performance Role")
        assert not _labels_match_for_merge("Rehearsal", "Rehearsing for The Drowsy Chaperone")

    def test_different_labels_should_drop_before_llm(self):
        """Spec: candidates with different labels never reach the LLM.

        Once implemented, the candidate-filtering pipeline should call
        _labels_match_for_merge between participant-scoring and the LLM
        call. Today it does not.
        """
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_label_equality,
            )
        except ImportError:
            pytest.fail("_filter_candidates_by_label_equality not implemented")

        scored = [
            {"node": _StubNode(id="diff-label", label="Performance Role"),
             "overlap": 2, "jaccard": 1.0},
            {"node": _StubNode(id="same-label", label="Performance"),
             "overlap": 1, "jaccard": 0.5},
        ]
        out = _filter_candidates_by_label_equality(scored, new_label="Performance")
        assert {s["node"].id for s in out} == {"same-label"}

    # ---- LLM invocation: when ALL gates pass + label same ----

    # ---- Hard reject 2 (richer): "enough known" time-frame check ----
    #
    # The current Event-only date filter is too narrow. Per the spec
    # (Jukka 2026-05-03), the time-frame hard reject should fire whenever
    # ENOUGH date info is known to determine non-overlap — even if exact
    # dates aren't. Examples below. The helper that would implement this
    # (_filter_candidates_by_time_frame, applies to State/Event/Goal alike)
    # doesn't exist yet — these xfail with ImportError until it lands.

    def test_state_end_before_other_start_should_drop(self):
        """One state explicitly ENDED before the other's start = sequential
        instances. Same Marriage label, two separate marriages with a divorce
        gap between them. Different instances; do not merge."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_time_frame,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_time_frame not implemented")

        cand = _StubNode(
            id="ended-marriage",
            label="Marriage",
            start_date=_ts(2010, 1, 1),
            end_date=_ts(2018, 6, 1),
        )
        new_start = _ts(2023, 5, 1)
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_candidates_by_time_frame(
            scored, new_valid_from=new_start, new_valid_to=None,
        )
        assert out == []

    def test_other_end_before_new_start_should_drop(self):
        """Symmetric: candidate ended in the past, new proposal starts
        well after. Different sequential instances."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_time_frame,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_time_frame not implemented")

        cand = _StubNode(id="old-job", label="Employment",
                         start_date=_ts(2015, 1, 1),
                         end_date=_ts(2020, 12, 31))
        new_start = _ts(2024, 1, 1)
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_candidates_by_time_frame(
            scored, new_valid_from=new_start, new_valid_to=None,
        )
        assert out == []

    def test_year_mismatch_via_prose_should_drop(self):
        """Partial date info: prose says 'in 2023' on the candidate and
        'in 2025' on the new proposal. Years differ — different instances
        even without exact dates."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_time_frame,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_time_frame not implemented")

        cand = _StubNode(id="conf-2023", label="Conference",
                         start_date_prose="in 2023")
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_candidates_by_time_frame(
            scored,
            new_valid_from=None,
            new_valid_to=None,
            new_start_prose="in 2025",
        )
        assert out == []

    def test_overlapping_open_states_should_be_kept(self):
        """Both states are open-ended (start_date set, end_date None) and
        the windows overlap. Could be same persistent state — keep for LLM."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_time_frame,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_time_frame not implemented")

        cand = _StubNode(id="ongoing", label="Residence",
                         start_date=_ts(2020, 1, 1), end_date=None)
        new_start = _ts(2023, 6, 1)
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_candidates_by_time_frame(
            scored, new_valid_from=new_start, new_valid_to=None,
        )
        assert len(out) == 1

    def test_both_dateless_pass_through_to_llm(self):
        """Both completely dateless: time-frame check has no evidence to
        dismiss on. Pass through to LLM. (This is INTENTIONAL — the
        dateless-bypass for Events was wrong because the new proposal HAD
        dates; here neither side does.)"""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_time_frame,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_time_frame not implemented")

        cand = _StubNode(id="dateless", label="Habit", start_date=None)
        scored = [{"node": cand, "overlap": 1, "jaccard": 1.0}]
        out = _filter_candidates_by_time_frame(
            scored, new_valid_from=None, new_valid_to=None,
        )
        assert len(out) == 1

    # ---- Hard reject 3 (refined): hub-overlap-only is too weak ----
    #
    # The actual 2026-05-03 Performance over-merge happened with a Jaccard
    # of 1/6 ≈ 0.17 — only "Jukka's children" overlapped (the most hub-y
    # entity in the personal graph). Today's filter accepts ANY non-zero
    # overlap. The spec says: weak overlap is evidence the candidates are
    # different, not similar.
    #
    # Two cuts at the same problem; either would have prevented the
    # Performance case. Both are xfail — neither helper exists yet.

    # --- Cut A: minimum Jaccard threshold (cheap, deterministic) ---

    def test_jaccard_below_threshold_should_drop(self):
        """Single shared participant out of 6 total = 0.167. Below a 0.5
        threshold → drop without LLM. Catches the Performance case."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_min_jaccard,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_min_jaccard not implemented")

        cand = _StubNode(id="weak-overlap")
        # Existing has 5 participants, new has 2 — only 1 shared.
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"hub", "p1", "p2", "p3", "p4"})],
            new_participant_ids={"hub", "newthing"},
        )
        # Sanity: jaccard should be ~0.167
        assert scored[0]["jaccard"] == pytest.approx(1 / 6, rel=0.01)
        out = _filter_candidates_by_min_jaccard(scored, threshold=0.5)
        assert out == []

    def test_jaccard_above_threshold_kept(self):
        """2 shared / 3 total = 0.667 > 0.5 → kept for LLM."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_min_jaccard,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_min_jaccard not implemented")

        cand = _StubNode(id="strong-overlap")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"alice", "bob"})],
            new_participant_ids={"alice", "bob", "carol"},
        )
        assert scored[0]["jaccard"] == pytest.approx(2 / 3, rel=0.01)
        out = _filter_candidates_by_min_jaccard(scored, threshold=0.5)
        assert len(out) == 1

    def test_actual_performance_case_via_jaccard_threshold(self):
        """The exact 2026-05-03 over-merge data, run through a 0.5 Jaccard
        threshold. Existing Performance hub: {children, SLMS, Annika, Jorma,
        Seija}. New Drowsy Chaperone proposal: {children, Drowsy Chaperone}.
        Overlap = {children}, union = 6 → 0.167. Threshold drops it."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_min_jaccard,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_min_jaccard not implemented")

        beetlejuice_hub = _StubNode(id="0b4416dd", label="Performance")
        existing_participants = {
            "467c96e6",  # Jukka's children
            "08eb7b81",  # South Lake Middle School
            "5397189b",  # Annika
            "41fb3423",  # Jorma
            "495f38f8",  # Seija
        }
        new_participants = {
            "467c96e6",  # Jukka's children (the only overlap — and it's the hub)
            "06c15571",  # The Drowsy Chaperone (never in existing)
        }
        scored = _score_candidates_by_participant_overlap(
            [(beetlejuice_hub, existing_participants)],
            new_participant_ids=new_participants,
        )
        assert scored[0]["jaccard"] == pytest.approx(1 / 6, rel=0.01)
        out = _filter_candidates_by_min_jaccard(scored, threshold=0.5)
        assert out == [], (
            "The Drowsy Chaperone-into-Performance over-merge would have "
            "been prevented by a Jaccard threshold of 0.5."
        )

    # --- Cut B: hub-weighted overlap (more principled, needs degrees) ---

    def test_hub_only_overlap_should_drop(self):
        """Sharing one hub-entity (high edge-count) is near-zero evidence.
        Hub-weighted overlap dismisses candidates whose only shared
        participants are hubs."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_weighted_overlap,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_weighted_overlap not implemented")

        cand = _StubNode(id="hub-only-shared")
        # The shared "hub_entity" connects to hundreds of nodes; the new
        # proposal's other participant is a low-degree specific entity.
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"hub_entity"})],
            new_participant_ids={"hub_entity", "specific_low_degree"},
        )
        out = _filter_candidates_by_weighted_overlap(
            scored,
            entity_degrees={"hub_entity": 500, "specific_low_degree": 2},
            min_weighted_score=0.1,
        )
        assert out == [], (
            "A hub-only overlap (degree=500) carries near-zero evidence "
            "of being the same node. Should drop."
        )

    def test_low_degree_overlap_kept(self):
        """Sharing a low-degree (specific) entity is strong evidence —
        keep for LLM."""
        try:
            from app.assistant.kg.proposal_promoter import (
                _filter_candidates_by_weighted_overlap,
            )
        except ImportError:
            pytest.xfail("_filter_candidates_by_weighted_overlap not implemented")

        cand = _StubNode(id="specific-shared")
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"specific_low_degree", "another_thing"})],
            new_participant_ids={"specific_low_degree", "yet_another"},
        )
        out = _filter_candidates_by_weighted_overlap(
            scored,
            entity_degrees={
                "specific_low_degree": 2,
                "another_thing": 5,
                "yet_another": 3,
            },
            min_weighted_score=0.1,
        )
        assert len(out) == 1

    def test_full_decision_tree_path_to_llm(self):
        """Positive case: type matches (precondition), dates close, overlap
        positive, label same → candidate survives all deterministic stages
        and would be passed to the LLM merger.

        This already works today through participant-score + date-filter.
        After the label-equality gate is added, it should still work
        because the labels DO match in this scenario.
        """
        new_date = _ts(2026, 5, 1)
        cand = _StubNode(id="match-me", label="Performance",
                         start_date=_ts(2026, 5, 2))
        scored = _score_candidates_by_participant_overlap(
            [(cand, {"annika", "peter"})],
            new_participant_ids={"annika", "peter"},
        )
        out = _filter_event_candidates_by_date(scored, new_valid_from=new_date)
        assert len(out) == 1 and out[0]["node"].id == "match-me"
        # Future label gate — would also keep this candidate (labels match):
        assert _labels_match_for_merge(out[0]["node"].label, "Performance")
