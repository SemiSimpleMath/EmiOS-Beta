"""Identity phase 4 — dup-scan Tier 5 (2026-06-12).

Tier 5a: normalized-equal identity sentences (same type) pair for triage.
Tier 5b: identity-embedding cosine gated by the recurring-event
date-separation test (calibration: same-shaped dated recurrences score
0.96+ while being distinct — the date gate, not the threshold, separates
them). Triage gains the defective_sentences verdict: sentences that fail
to discriminate get nulled for regeneration, nodes stay unstamped.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan as scan
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_core.kg_utils.date_compare import dates_separated
from app.models.base import get_session

D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731


def _desc(nid, label="N", ntype="Event", identity="", start=None):
    return {
        "node_id": nid, "label": label, "node_type": ntype,
        "category": "", "aliases": [], "description": "",
        "semantic_label": "", "original_sentence": "",
        "identity_sentence": identity, "start_date": start, "end_date": None,
        "edge_sentences": [], "edge_count": 0, "neighborhood": [],
    }


# ── dates_separated ──────────────────────────────────────────────────────


def test_dates_separated_doctrine():
    assert dates_separated("2026-04-04", "2026-03-16")          # recurrence
    assert not dates_separated("2026-04-04", "2026-04-05")      # same-ish
    assert not dates_separated("2026-04-04", None)              # unknown sinks
    assert not dates_separated(None, None)
    assert dates_separated(D(2026, 4, 4), D(2026, 3, 16))        # datetimes too


# ── Tier 5a: exact ───────────────────────────────────────────────────────


def test_identity_exact_pairs_same_type_only():
    descs = {
        "a": _desc("a", "The Matrix", "Entity", "the movie the user prefers"),
        "b": _desc("b", "Annie Hall", "Entity", "The movie the user prefers."),
        "c": _desc("c", "Some Event", "Event", "the movie the user prefers"),
        "d": _desc("d", "Unique", "Entity", "a one-of-a-kind sentence"),
        "e": _desc("e", "Empty", "Entity", ""),
    }
    pairs = scan._identity_exact_pairs(descs)
    assert pairs == [("a", "b")]  # normalization unifies; cross-type excluded


# ── Tier 5b: similarity + date gate ──────────────────────────────────────


class _FakeIdentityCollection:
    def __init__(self, ids, embs):
        self._ids, self._embs = ids, embs

    def get(self, include=None):
        return {"ids": self._ids, "embeddings": self._embs}


def test_identity_similarity_respects_date_separation(monkeypatch):
    descs = {
        "dup1": _desc("dup1", "Sibling Rel", "State",
                      "the sibling relationship", start="2013-08-12"),
        "dup2": _desc("dup2", "Sibling Rel", "State",
                      "the sibling relationship since 2013", start=None),
        "rec1": _desc("rec1", "Greeting", "Event",
                      "greeting on April 4", start="2026-04-04"),
        "rec2": _desc("rec2", "Greeting", "Event",
                      "greeting on March 16", start="2026-03-16"),
    }
    # dup1~dup2 similar; rec1~rec2 VERY similar but date-separated.
    embs = {
        "dup1": [1.0, 0.05, 0.0], "dup2": [1.0, 0.0, 0.05],
        "rec1": [0.0, 1.0, 0.01], "rec2": [0.0, 1.0, 0.0],
    }
    ids = list(embs)
    fake_cm = SimpleNamespace(
        node_identity_collection=_FakeIdentityCollection(ids, [embs[i] for i in ids]),
    )
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake_cm)

    pairs = scan._identity_similarity_pairs(descs)
    assert ("dup1", "dup2") in pairs          # undated side → date gate sinks open
    assert ("rec1", "rec2") not in pairs      # distinct recurrence by doctrine


# ── triage defective_sentences verdict ───────────────────────────────────


def _mk_node(label, identity):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Entity",
                         identity_sentence=identity,
                         identity_inputs_hash="stale-hash"))
        session.commit()
    finally:
        session.close()
    return nid


def test_triage_defective_verdict_nulls_sentences_and_unstamps(monkeypatch):
    a = _mk_node("The Matrix", "the movie the user prefers")
    b = _mk_node("Annie Hall", "the movie the user prefers")
    descs = {
        a: _desc(a, "The Matrix", "Entity", "the movie the user prefers"),
        b: _desc(b, "Annie Hall", "Entity", "the movie the user prefers"),
    }
    pairs = [(min(a, b), max(a, b), "identity_exact")]

    class _FakeAgent:
        def action_handler(self, message):
            return SimpleNamespace(data={"pairs": [
                {"pair_index": 1, "verdict": "defective_sentences",
                 "reason": "identical referent-free sentences on different films"},
            ]})

    monkeypatch.setattr(
        scan, "DI", SimpleNamespace(
            agent_factory=SimpleNamespace(create_agent=lambda name: _FakeAgent())),
    )

    survivors, defective = scan._triage_filter_pairs(pairs, descs, None, {})
    assert survivors == []
    assert defective == {a, b}

    session = get_session()
    try:
        for nid in (a, b):
            n = session.get(Node, nid)
            assert n.identity_sentence is None
            assert n.identity_inputs_hash is None  # regenerates next nightly
    finally:
        session.close()
