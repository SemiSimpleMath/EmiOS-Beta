"""Belief-engine v2 ingestion seam (§3a), step 2: enriched ActionableItems -> v2 observation log.

Exercises the ingest module with a fake embedder and NO verifier (no LLM): identical claims collapse
to one belief via exact-alias resolution (two justifications), items without an extracted_claim are
skipped, distinct claims stay distinct, and a contradict observation registers against the belief.
Writes to a temp db so the real shadow store is never touched.
"""
from __future__ import annotations

from belief_engine_v2.ingest import consolidate_live_store, ingest_items, open_live_store


def _store(tmp_path):
    # Non-degenerate fake embedder (well-defined norm); no verifier => never auto-merges.
    return open_live_store(
        str(tmp_path / "v2.db"),
        embedder=lambda t: [1.0, float(len(t or "") % 5)],
        with_verifier=False,
    )


def _item(subj, pred, obj, *, polarity="affirm", claim=True):
    item = {"fact_summary": "f", "tags": [], "evidence": ["e"]}
    if claim:
        item["extracted_claim"] = {
            "subject_phrase": subj, "predicate_phrase": pred, "object_phrase": obj,
            "polarity": polarity,
        }
    return item


def test_identical_claims_collapse_to_one_belief(tmp_path):
    store = _store(tmp_path)
    items = [
        _item("the user", "prefers", "coffee before 11am"),
        _item("the user", "prefers", "coffee before 11am"),
    ]
    assert ingest_items(store, items, occurred_at="2026-06-09") == {"appended": 2, "skipped": 0}
    beliefs = store.beliefs()
    assert len(beliefs) == 1                                   # one canonical belief
    assert len(store.justifications_for(beliefs[0]["belief_id"])) == 2   # both observations attached
    store.close()


def test_item_without_claim_is_skipped(tmp_path):
    store = _store(tmp_path)
    summary = ingest_items(
        store, [_item("a", "b", "c"), _item(None, None, None, claim=False)], occurred_at="2026-06-09",
    )
    assert summary == {"appended": 1, "skipped": 1}
    assert len(store.beliefs()) == 1
    store.close()


def test_distinct_claims_stay_distinct(tmp_path):
    store = _store(tmp_path)
    items = [
        _item("the user", "prefers", "coffee before 11am"),
        _item("the kids", "wont eat", "zucchini"),
    ]
    ingest_items(store, items, occurred_at="2026-06-09")
    assert len(store.beliefs()) == 2
    store.close()


def test_consolidation_sweep_merges_residual_near_dups(tmp_path):
    # Two different object phrasings of the same belief -> write-time (no verifier) mints TWO beliefs;
    # the #6 consolidation sweep (embedding proposes the pair, verifier says "same") collapses them.
    store = _store(tmp_path)
    items = [
        _item("the user", "prefers", "coffee in the morning"),
        _item("the user", "prefers", "a morning cup of coffee"),
    ]
    ingest_items(store, items, occurred_at="2026-06-09")
    assert len(store.beliefs()) == 2                       # residual fragmentation at write time
    res = consolidate_live_store(store, embedder=lambda t: [1.0, 0.0], verifier=lambda a, b, k: True)
    assert res["merges"] == 1
    assert len(store.beliefs()) == 1                       # collapsed to one canonical belief
    store.close()


def test_consolidation_leaves_distinct_beliefs_alone(tmp_path):
    # Verifier says "not the same" -> nothing merges (safe: similarity alone never merges).
    store = _store(tmp_path)
    items = [
        _item("the user", "prefers", "coffee in the morning"),
        _item("the user", "prefers", "a morning cup of coffee"),
    ]
    ingest_items(store, items, occurred_at="2026-06-09")
    res = consolidate_live_store(store, embedder=lambda t: [1.0, 0.0], verifier=lambda a, b, k: False)
    assert res["merges"] == 0
    assert len(store.beliefs()) == 2
    store.close()


def test_contradict_registers_against_belief(tmp_path):
    store = _store(tmp_path)
    items = [
        _item("the user", "prefers", "coffee before 11am"),
        _item("the user", "prefers", "coffee before 11am", polarity="contradict"),
    ]
    ingest_items(store, items, occurred_at="2026-06-09")
    beliefs = store.beliefs()
    assert len(beliefs) == 1
    assert beliefs[0]["support"] >= 1 and beliefs[0]["contradiction"] >= 1
    store.close()
