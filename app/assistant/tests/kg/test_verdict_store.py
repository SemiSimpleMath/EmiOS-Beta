"""Unit tests for `kg_maintenance.verdict_store`.

Covers the contract surface: canonical pair ordering, write rejection on
unknown vocabulary, write rejection on too many ids, single-node vs
pairwise lookup, supersede, and the bulk `load_distinct_pairs` helper.

The conftest in this directory recreates Base.metadata tables before
each test, so kg_node_verdict is fresh per case.
"""
from __future__ import annotations

import pytest

from app.assistant.database.kg_node_verdict import KGNodeVerdict  # noqa: F401  table registration
from app.assistant.kg_maintenance.verdict_store import (
    canonical_pair,
    get_verdicts_for_node,
    get_verdicts_for_pair,
    is_pair_marked_distinct,
    load_distinct_pairs,
    record_verdict,
    supersede_verdict,
)


def test_canonical_pair_orders_lexicographically():
    assert canonical_pair("Z", "A") == ("A", "Z")
    assert canonical_pair("A", "Z") == ("A", "Z")


def test_canonical_pair_collapses_self_pair():
    """(X, X) should collapse to single-node form (X, None) so we never
    write a row that no lookup hits."""
    assert canonical_pair("X", "X") == ("X", None)


def test_canonical_pair_passthrough_single_node():
    assert canonical_pair("X", None) == ("X", None)
    assert canonical_pair("X", "") == ("X", None)


def test_record_verdict_pair_canonicalizes_on_write():
    vid = record_verdict(
        verdict_type="distinct",
        memo="do not merge X with Y",
        node_ids=["Y", "X"],  # deliberately reversed
        decided_by="test",
    )
    assert vid

    # Lookup in either order must hit the same row.
    hits1 = get_verdicts_for_pair("X", "Y")
    hits2 = get_verdicts_for_pair("Y", "X")
    assert len(hits1) == 1
    assert hits1[0].id == hits2[0].id
    assert hits1[0].node_id_a == "X"  # canonical
    assert hits1[0].node_id_b == "Y"


def test_record_verdict_rejects_unknown_type():
    """Unknown verdict_type is a contract violation, not an extension
    point — downstream filters match on the closed vocabulary."""
    vid = record_verdict(
        verdict_type="nonsense",
        memo="m",
        node_ids=["a", "b"],
        decided_by="test",
    )
    assert vid is None


def test_record_verdict_rejects_three_or_more_ids():
    """Schema row holds at most a pair; caller must split or single."""
    vid = record_verdict(
        verdict_type="distinct",
        memo="m",
        node_ids=["a", "b", "c"],
        decided_by="test",
    )
    assert vid is None


def test_record_verdict_rejects_empty_memo():
    vid = record_verdict(
        verdict_type="distinct",
        memo="",
        node_ids=["a", "b"],
        decided_by="test",
    )
    assert vid is None


def test_record_verdict_rejects_empty_node_ids():
    vid = record_verdict(
        verdict_type="verified",
        memo="m",
        node_ids=[],
        decided_by="test",
    )
    assert vid is None


def test_single_node_verdict_writes_with_b_null():
    vid = record_verdict(
        verdict_type="verified",
        memo="start_date confirmed",
        node_ids=["abc"],
        decided_by="test",
    )
    assert vid
    hits = get_verdicts_for_node("abc")
    assert len(hits) == 1
    assert hits[0].node_id_a == "abc"
    assert hits[0].node_id_b is None


def test_is_pair_marked_distinct_is_order_insensitive():
    record_verdict(
        verdict_type="distinct", memo="m",
        node_ids=["A", "B"], decided_by="t",
    )
    assert is_pair_marked_distinct("A", "B")
    assert is_pair_marked_distinct("B", "A")
    assert not is_pair_marked_distinct("A", "C")


def test_is_pair_marked_distinct_only_matches_distinct():
    """A 'verified' verdict on a pair shouldn't trigger the distinct filter."""
    # (Hypothetical — we don't write 'verified' as pair today, but the
    # filter should be type-strict either way.)
    record_verdict(
        verdict_type="false_positive", memo="m",
        node_ids=["A", "B"], decided_by="t",
    )
    assert not is_pair_marked_distinct("A", "B")


def test_supersede_verdict_hides_from_active_lookups():
    vid = record_verdict(
        verdict_type="distinct", memo="m",
        node_ids=["A", "B"], decided_by="t",
    )
    assert is_pair_marked_distinct("A", "B")
    assert supersede_verdict(vid, reason="test_supersede")
    assert not is_pair_marked_distinct("A", "B")
    # But include_superseded=True still finds it.
    hits = get_verdicts_for_pair("A", "B", include_superseded=True)
    assert len(hits) == 1
    assert hits[0].superseded_at is not None


def test_load_distinct_pairs_returns_canonical_set():
    record_verdict(
        verdict_type="distinct", memo="m1",
        node_ids=["B", "A"], decided_by="t",
    )
    record_verdict(
        verdict_type="distinct", memo="m2",
        node_ids=["X", "Y"], decided_by="t",
    )
    # Single-node and non-distinct should not appear.
    record_verdict(
        verdict_type="verified", memo="m3",
        node_ids=["solo"], decided_by="t",
    )
    record_verdict(
        verdict_type="false_positive", memo="m4",
        node_ids=["P", "Q"], decided_by="t",
    )

    pairs = load_distinct_pairs()
    assert ("A", "B") in pairs
    assert ("X", "Y") in pairs
    assert ("solo", None) not in pairs
    assert ("P", "Q") not in pairs


def test_load_distinct_pairs_excludes_superseded():
    vid = record_verdict(
        verdict_type="distinct", memo="m",
        node_ids=["A", "B"], decided_by="t",
    )
    supersede_verdict(vid, reason="x")
    assert ("A", "B") not in load_distinct_pairs()


def test_get_verdicts_for_node_finds_via_either_position():
    record_verdict(
        verdict_type="distinct", memo="m",
        node_ids=["alpha", "beta"], decided_by="t",
    )
    # Stored canonically (alpha, beta). Looking up beta should still
    # find it via node_id_b match.
    hits_a = get_verdicts_for_node("alpha")
    hits_b = get_verdicts_for_node("beta")
    assert len(hits_a) == 1
    assert len(hits_b) == 1
    assert hits_a[0].id == hits_b[0].id
