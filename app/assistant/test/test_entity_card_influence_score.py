"""
Unit tests for the two-pass entity influence scoring logic.

Pass 1 — raw degree: in-edges + out-edges per node.
Pass 2 — influence score: sum of neighbor degrees.
Adjusted score — influence / sqrt(word_count).

The motivation: entities in this KG never connect directly.
A person A connects to person B through an intermediate state node S:

    A --[has_son]--> S --[target]--> B

This means B's raw degree is low (just the one edge from S), but S
itself has high degree (because A is highly connected).  Pass 2
captures this: B's influence score = degree(S), which is high.

The sqrt word-count penalty means a single-word label like "Jukka" is
not penalised relative to "Friday Night Meats" (3 words), but the
verbose label still needs meaningfully higher raw influence to survive
the gate at the same adjusted score.

These tests exercise the scoring functions in isolation with a fake
edge list, verifying the algorithm is correct before wiring it into
the pipeline.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Implementation under test (inline here so the test is standalone).
# Once validated, this will be extracted into the pipeline module.
# ---------------------------------------------------------------------------

def compute_degree(edges: List[Tuple[str, str]]) -> Dict[str, int]:
    """
    Pass 1.

    Args:
        edges: list of (source_id, target_id) pairs.

    Returns:
        Dict mapping node_id -> total degree (in + out).
    """
    degree: Dict[str, int] = {}
    for src, tgt in edges:
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1
    return degree


def compute_influence(
    edges: List[Tuple[str, str]],
    degree: Dict[str, int],
) -> Dict[str, float]:
    """
    Pass 2.

    For each node, sum the degrees of all its direct neighbors.
    A node whose neighbors are highly connected scores high even if its
    own raw degree is low (e.g. an entity reached only through a
    high-degree state node).

    Args:
        edges:  same (source_id, target_id) list used in Pass 1.
        degree: output of compute_degree().

    Returns:
        Dict mapping node_id -> influence score.
    """
    influence: Dict[str, float] = {}
    for src, tgt in edges:
        influence[src] = influence.get(src, 0.0) + degree.get(tgt, 0)
        influence[tgt] = influence.get(tgt, 0.0) + degree.get(src, 0)
    return influence


def adjusted_influence(raw_influence: float, label: str) -> float:
    """Apply a sqrt word-count penalty: score / sqrt(word_count)."""
    words = max(1, len((label or "").strip().split()))
    return raw_influence / math.sqrt(words)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _graph(*edges: Tuple[str, str]) -> List[Tuple[str, str]]:
    return list(edges)


# ---------------------------------------------------------------------------
# Tests — degree
# ---------------------------------------------------------------------------

def test_degree_single_edge():
    edges = _graph(("A", "B"))
    d = compute_degree(edges)
    assert d["A"] == 1
    assert d["B"] == 1


def test_degree_star_topology():
    """Hub node H connects to four leaves."""
    edges = _graph(("H", "L1"), ("H", "L2"), ("H", "L3"), ("H", "L4"))
    d = compute_degree(edges)
    assert d["H"] == 4
    assert d["L1"] == d["L2"] == d["L3"] == d["L4"] == 1


def test_degree_counts_both_directions():
    """Incoming and outgoing edges both contribute."""
    edges = _graph(("X", "M"), ("M", "Y"), ("M", "Z"))
    d = compute_degree(edges)
    # M has 3 edges total: 1 in (from X), 2 out (to Y, Z)
    assert d["M"] == 3
    assert d["X"] == 1
    assert d["Y"] == d["Z"] == 1


# ---------------------------------------------------------------------------
# Tests — influence
# ---------------------------------------------------------------------------

def test_influence_leaf_through_high_degree_state():
    """
    Core scenario: A -[has_son]-> S -[points_to]-> B
    S is also connected to many other nodes, making it high-degree.
    B's raw degree is 1 but its influence score should reflect S's degree.
    """
    edges = _graph(
        ("A", "S"),   # A -> state node S
        ("S", "B"),   # state node S -> B (the "son")
        ("S", "C"),   # S also connects to other things
        ("S", "D"),
        ("S", "E"),
    )
    d = compute_degree(edges)
    assert d["S"] == 5   # 1 in (from A) + 4 out (to B, C, D, E)
    assert d["B"] == 1   # only one edge: from S

    inf = compute_influence(edges, d)

    # B's only neighbor is S (degree 5), so influence = 5
    assert inf["B"] == d["S"]

    # C, D, E are in the same position as B
    assert inf["C"] == inf["D"] == inf["E"] == d["S"]

    # A's only neighbor is S, so A's influence = degree(S) = 5
    assert inf["A"] == d["S"]

    # S's influence = degree(A) + degree(B) + degree(C) + degree(D) + degree(E)
    expected_s = d["A"] + d["B"] + d["C"] + d["D"] + d["E"]
    assert inf["S"] == expected_s


def test_influence_isolated_node_not_present():
    """A node with no edges appears in neither map."""
    edges = _graph(("X", "Y"))
    d = compute_degree(edges)
    inf = compute_influence(edges, d)
    assert "Z" not in d
    assert "Z" not in inf


def test_influence_chain():
    """
    Linear chain: A -> B -> C -> D
    Influence should flow from the ends toward the middle.
    """
    edges = _graph(("A", "B"), ("B", "C"), ("C", "D"))
    d = compute_degree(edges)
    # A=1, B=2, C=2, D=1
    assert d == {"A": 1, "B": 2, "C": 2, "D": 1}

    inf = compute_influence(edges, d)
    # A's neighbors: B(2)          -> 2
    # B's neighbors: A(1), C(2)    -> 3
    # C's neighbors: B(2), D(1)    -> 3
    # D's neighbors: C(2)          -> 2
    assert inf["A"] == 2
    assert inf["B"] == 3
    assert inf["C"] == 3
    assert inf["D"] == 2


def test_influence_highly_connected_hub_raises_leaf_scores():
    """
    Hub H has 10 outgoing edges.  Each leaf L_i has degree 1.
    Each leaf's influence score should equal degree(H) = 10.
    """
    leaves = [f"L{i}" for i in range(10)]
    edges = _graph(*[("H", leaf) for leaf in leaves])
    d = compute_degree(edges)
    assert d["H"] == 10
    inf = compute_influence(edges, d)
    for leaf in leaves:
        assert inf[leaf] == 10, f"{leaf} influence should be 10, got {inf[leaf]}"


def test_influence_scores_higher_than_degree_for_well_connected_leaves():
    """
    The whole point: a leaf attached to a high-degree hub has a higher
    influence score than its raw degree.
    """
    edges = _graph(
        ("HUB", "LEAF"),
        ("HUB", "X1"), ("HUB", "X2"), ("HUB", "X3"), ("HUB", "X4"),
        ("HUB", "X5"), ("HUB", "X6"), ("HUB", "X7"), ("HUB", "X8"),
    )
    d = compute_degree(edges)
    inf = compute_influence(edges, d)

    assert d["LEAF"] == 1
    assert inf["LEAF"] == d["HUB"]
    assert inf["LEAF"] > d["LEAF"]


# ---------------------------------------------------------------------------
# Tests — adjusted influence (sqrt word-count penalty)
# ---------------------------------------------------------------------------

def test_adjusted_influence_single_word_no_penalty():
    """A single-word label is not penalised: adjusted == raw."""
    assert adjusted_influence(100.0, "Jukka") == pytest.approx(100.0)


def test_adjusted_influence_two_words():
    """Two-word label is divided by sqrt(2) ≈ 1.414."""
    result = adjusted_influence(100.0, "Friday Meats")
    assert result == pytest.approx(100.0 / math.sqrt(2))


def test_adjusted_influence_three_words():
    """Three-word label divided by sqrt(3) ≈ 1.732."""
    result = adjusted_influence(100.0, "Friday Night Meats")
    assert result == pytest.approx(100.0 / math.sqrt(3))


def test_adjusted_influence_empty_label_treated_as_one_word():
    """Empty / whitespace label defaults to 1 word — no division by zero."""
    assert adjusted_influence(50.0, "") == pytest.approx(50.0)
    assert adjusted_influence(50.0, "   ") == pytest.approx(50.0)


def test_adjusted_influence_single_word_beats_multi_word_same_raw():
    """
    With equal raw influence, a single-word label has a higher adjusted score
    than a multi-word label — the penalty correctly favours concise names.
    """
    single = adjusted_influence(85.0, "Jukka")
    multi = adjusted_influence(85.0, "Friday Night Meats")
    assert single > multi


def test_adjusted_influence_high_raw_multi_word_can_beat_low_raw_single():
    """
    A multi-word label with much higher raw influence can still outscore a
    single-word label with low raw influence — the penalty is soft, not a ban.
    Friday Night Meats (inf=85, 3 words) vs some node (inf=20, 1 word).
    """
    fnm = adjusted_influence(85.0, "Friday Night Meats")   # 85 / sqrt(3) ≈ 49.1
    other = adjusted_influence(20.0, "Conversation")        # 20 / 1 = 20.0
    assert fnm > other


import pytest  # noqa: E402  (placed after the test functions intentionally)
