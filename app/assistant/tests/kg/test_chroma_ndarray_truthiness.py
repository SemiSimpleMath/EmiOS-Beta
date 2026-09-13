"""Chroma results are numpy arrays; `x or []` on one raises and takes a pipeline down.

2026-08-24 22:43:15, verbatim from the log:

    [KGMaintenancePipeline] duplicate_scan failed:
      The truth value of an array with more than one element is ambiguous
    step_duplicate_scan.py:661, in _identity_similarity_pairs
      embs = np.array(res.get("embeddings") or [], dtype=np.float32)
    ValueError

chromadb >= 1.x returns embeddings as an ndarray. `x or []` evaluates bool(x), and numpy
refuses that for anything with more than one element. Three consecutive failures
auto-disabled kg_maintenance_pipeline, which is the ONLY producer of duplicate_node
findings. The duplicate merger runs daily but merely drains those findings, so with the
producer off it had nothing to do and the graph carried unmerged duplicate Entity nodes for
four household members for 18 days -- which in turn broke the noticer's household digest,
because its lookup uses .one_or_none() on a label.

The bug is the idiom, not the line. These tests pin the shapes rather than the callers.
"""
from __future__ import annotations

import numpy as np
import pytest


def test_the_idiom_itself_raises_on_a_chroma_style_result():
    """Guard the diagnosis: this is what the old code did."""
    embeddings = np.zeros((3, 8), dtype=np.float32)
    with pytest.raises(ValueError, match="truth value of an array"):
        _ = embeddings or []


def test_size_based_checks_do_not_raise():
    for value in (np.zeros((3, 8)), np.zeros((1, 8)), np.zeros((0, 8)), [], None):
        empty = value is None or len(value) == 0     # the shape both fixes now use
        assert empty in (True, False)


@pytest.mark.parametrize("embeddings", [
    np.zeros((4, 6), dtype=np.float32),      # the real case: ndarray, many rows
    np.zeros((1, 6), dtype=np.float32),      # single row still has ndim 2
    [[0.0] * 6, [1.0] * 6],                  # older chroma returned lists
])
def test_identity_similarity_pairs_survives_every_result_shape(monkeypatch, embeddings):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_duplicate_scan as s

    ids = [f"n{i}" for i in range(len(embeddings))]

    class _Collection:
        def get(self, include=None):
            return {"ids": ids, "embeddings": embeddings}

    class _Manager:
        node_identity_collection = _Collection()

    monkeypatch.setattr(
        "app.assistant.kg.chroma.chroma_embedding_manager.get_chroma_manager",
        lambda: _Manager(), raising=False)

    descriptors = {i: {"label": i, "node_type": "Entity"} for i in ids}
    # Must not raise. The pairing result itself depends on thresholds and is not the point.
    out = s._identity_similarity_pairs(descriptors)
    assert isinstance(out, list)


@pytest.mark.parametrize("embeddings", [
    np.zeros((0, 6), dtype=np.float32),
    [],
    None,
])
def test_an_empty_identity_collection_returns_no_pairs(monkeypatch, embeddings):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_duplicate_scan as s

    class _Collection:
        def get(self, include=None):
            return {"ids": ["a", "b"], "embeddings": embeddings}

    class _Manager:
        node_identity_collection = _Collection()

    monkeypatch.setattr(
        "app.assistant.kg.chroma.chroma_embedding_manager.get_chroma_manager",
        lambda: _Manager(), raising=False)

    descriptors = {"a": {"label": "a"}, "b": {"label": "b"}}
    assert s._identity_similarity_pairs(descriptors) == []


def test_the_promoter_reads_query_results_the_same_safe_way():
    """proposal_promoter.py carried the identical idiom on a query() result."""
    import inspect

    from app.assistant.kg import proposal_promoter

    src = inspect.getsource(proposal_promoter)
    assert 'res.get("embeddings") or' not in src, (
        "the ndarray-unsafe `or` idiom is back in proposal_promoter")
