"""Identity sentences — graph-derived definite descriptions (2026-06-12).

The identity layer's v1: gather (deterministic facts), hash (staleness on
STRUCTURE only — prose churn must not regenerate), refresh (NULL-first
backfill, cap, updated_at preserved verbatim), and the semantic tier
querying the identity collection with the mention's own sentence.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_core.kg_utils.identity_sentence import (
    gather_identity_inputs,
    identity_inputs_hash,
    run_identity_sentence_refresh,
)
from app.models.base import get_session

D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731


def _mk_node(label, node_type="Entity", **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id, target_id, rel, importance=None):
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(id=eid, source_id=source_id, target_id=target_id,
                         relationship_type=rel, importance=importance))
        session.commit()
    finally:
        session.close()
    return eid


def _inputs_for(nid):
    session = get_session()
    try:
        return gather_identity_inputs(session, session.get(Node, nid))
    finally:
        session.close()


# ── gather + hash ────────────────────────────────────────────────────────


def test_gather_renders_anchors_dates_and_skips_pod_endpoints():
    house = _mk_node("Tom's House", category="residence",
                     start_date=D(2010, 6, 1), start_date_confidence="estimated")
    owner = _mk_node("Tom")
    town = _mk_node("Springfield")
    _mk_edge(house, town, "located_in", importance=8.0)
    _mk_edge(owner, house, "owns")
    _mk_edge(house, "datapod:image:abcdef123456", "depicted_in")  # pod endpoint

    inputs = _inputs_for(house)
    assert inputs["label"] == "Tom's House"
    assert inputs["start_date"] == "2010-06"  # month floor, honest precision
    joined = "\n".join(inputs["edges"])
    assert "located_in" in joined and "Springfield" in joined
    assert "Tom" in joined
    assert "datapod" not in joined  # pod endpoints skipped


def test_hash_tracks_structure_not_prose():
    house = _mk_node("Tom's House", description="a house")
    town = _mk_node("Springfield")
    _mk_edge(house, town, "located_in")
    h1 = identity_inputs_hash(_inputs_for(house))

    # Prose churn — description change must NOT move the hash.
    session = get_session()
    try:
        session.get(Node, house).description = "a lovely house with a garden"
        session.commit()
    finally:
        session.close()
    assert identity_inputs_hash(_inputs_for(house)) == h1

    # Structure change — a new anchor edge MUST move the hash.
    owner = _mk_node("Tom")
    _mk_edge(owner, house, "owns")
    assert identity_inputs_hash(_inputs_for(house)) != h1


# ── refresh ──────────────────────────────────────────────────────────────


class _FakeChroma:
    def __init__(self):
        self.stored = {}

    def store_node_identity_embedding(self, node_id, sentence, embedding):
        self.stored[node_id] = sentence


def _run_refresh(monkeypatch, sentences_by_label, max_per_run=50,
                 window_text=None):
    import app.assistant.kg_core.kg_utils.identity_sentence as mod

    calls = []

    def fake_generate(inputs, *, source_window=None):
        calls.append((inputs["label"], source_window))
        s = sentences_by_label.get(inputs["label"])
        return {"sentence": s, "definable": s is not None, "basis": "test"}

    fake_chroma = _FakeChroma()
    monkeypatch.setattr(mod, "generate_identity_sentence", fake_generate)
    monkeypatch.setattr(mod, "_source_window_text", lambda nid: window_text)
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake_chroma)
    import app.assistant.embeddings.embedder as embedder_mod
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: [0.1, 0.2])

    result = mod.run_identity_sentence_refresh(max_per_run=max_per_run)
    return result, [c[0] for c in calls], fake_chroma


def test_refresh_backfills_null_sentences_and_stamps_hash(monkeypatch):
    house = _mk_node("Tom's House")
    _mk_edge(house, _mk_node("Springfield"), "located_in")

    result, calls, chroma = _run_refresh(
        monkeypatch, {"Tom's House": "Tom's house in Springfield."})

    assert "Tom's House" in calls
    assert result["generated"] == 1
    session = get_session()
    try:
        n = session.get(Node, house)
        assert n.identity_sentence == "Tom's house in Springfield."
        assert n.identity_inputs_hash
        assert chroma.stored[house] == "Tom's house in Springfield."
    finally:
        session.close()


def test_refresh_skips_fresh_and_regenerates_stale(monkeypatch):
    fresh = _mk_node("Fresh Node")
    # Stamp it as current: sentence + matching hash.
    session = get_session()
    try:
        n = session.get(Node, fresh)
        n.identity_sentence = "Already current."
        n.identity_inputs_hash = identity_inputs_hash(_inputs_for(fresh))
        session.commit()
    finally:
        session.close()

    stale = _mk_node("Stale Node")
    session = get_session()
    try:
        n = session.get(Node, stale)
        n.identity_sentence = "Out of date."
        n.identity_inputs_hash = "old-hash"
        session.commit()
    finally:
        session.close()

    result, calls, _ = _run_refresh(
        monkeypatch, {"Stale Node": "Regenerated."})

    assert "Fresh Node" not in calls
    assert "Stale Node" in calls
    assert result["skipped_fresh"] >= 1
    session = get_session()
    try:
        assert session.get(Node, fresh).identity_sentence == "Already current."
        assert session.get(Node, stale).identity_sentence == "Regenerated."
    finally:
        session.close()


def test_refresh_preserves_updated_at_verbatim(monkeypatch):
    nid = _mk_node("Quiet Node")
    session = get_session()
    try:
        before = session.get(Node, nid).updated_at
    finally:
        session.close()

    _run_refresh(monkeypatch, {"Quiet Node": "A quiet node."})

    session = get_session()
    try:
        n = session.get(Node, nid)
        assert n.identity_sentence == "A quiet node."
        assert n.updated_at == before  # projection: no cascade trigger
    finally:
        session.close()


def test_refresh_respects_cap(monkeypatch):
    for i in range(4):
        _mk_node(f"Capped {i}")
    result, calls, _ = _run_refresh(
        monkeypatch, {f"Capped {i}": f"Node {i}." for i in range(4)},
        max_per_run=2,
    )
    assert len(calls) == 2
    assert result["generated"] == 2


def test_refresh_node_types_filter(monkeypatch):
    _mk_node("Some Entity")
    _mk_node("Some Trip", node_type="Event")
    _mk_node("Some Birthday", node_type="Property")  # never covered

    import app.assistant.kg_core.kg_utils.identity_sentence as mod

    calls = []

    def fake_generate(inputs, *, source_window=None):
        calls.append(inputs["label"])
        return {"sentence": "S.", "definable": True, "basis": "test"}

    monkeypatch.setattr(mod, "generate_identity_sentence", fake_generate)
    monkeypatch.setattr(mod, "_source_window_text", lambda nid: None)
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: _FakeChroma())
    import app.assistant.embeddings.embedder as embedder_mod
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: [0.1])

    mod.run_identity_sentence_refresh(max_per_run=50, node_types=("Event",))
    assert calls == ["Some Trip"]

    calls.clear()
    mod.run_identity_sentence_refresh(max_per_run=50)  # default: all covered
    assert "Some Entity" in calls and "Some Trip" not in calls  # Event now fresh
    assert "Some Birthday" not in calls  # Property excluded always


# ── escalation + undefinable verdict (user directive, 2026-06-12) ───────


def test_indefinable_escalates_to_source_window(monkeypatch):
    import app.assistant.kg_core.kg_utils.identity_sentence as mod

    nid = _mk_node("Vague Fragment")
    calls = []

    def fake_generate(inputs, *, source_window=None):
        calls.append(source_window)
        if source_window is None:
            return {"sentence": None, "definable": False, "basis": "no anchors"}
        return {"sentence": "The fragment from the window.", "definable": True,
                "basis": "window"}

    monkeypatch.setattr(mod, "generate_identity_sentence", fake_generate)
    monkeypatch.setattr(mod, "_source_window_text", lambda n: "user: blah blah")
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: _FakeChroma())
    import app.assistant.embeddings.embedder as embedder_mod
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: [0.1])

    result = mod.run_identity_sentence_refresh(max_per_run=5)

    assert calls == [None, "user: blah blah"]  # stage 1 then stage 2
    assert result["generated"] == 1
    session = get_session()
    try:
        assert session.get(Node, nid).identity_sentence == "The fragment from the window."
    finally:
        session.close()


def test_undefinable_stamps_hash_raises_finding_and_does_not_retry(monkeypatch):
    import app.assistant.kg_core.kg_utils.identity_sentence as mod
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding

    nid = _mk_node("Pure Junk Fragment")

    def fake_generate(inputs, *, source_window=None):
        return {"sentence": None, "definable": False, "basis": "no referent"}

    monkeypatch.setattr(mod, "generate_identity_sentence", fake_generate)
    monkeypatch.setattr(mod, "_source_window_text", lambda n: "user: noise")
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: _FakeChroma())
    import app.assistant.embeddings.embedder as embedder_mod
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: [0.1])

    result = mod.run_identity_sentence_refresh(max_per_run=5)
    assert result["undefinable"] == 1

    session = get_session()
    try:
        n = session.get(Node, nid)
        assert n.identity_sentence is None
        assert n.identity_inputs_hash  # verdict stamped
        f = (session.query(KGMaintenanceFinding)
             .filter_by(finding_type="undefinable_node", primary_node_id=nid)
             .one())
        report = dict(f.investigation_report_json or {})
        assert f.status == "investigated"
        assert report.get("disposition") == "needs_user_review"
    finally:
        session.close()

    # Second run: hash matches -> skipped, no second writer call, no dup finding.
    result2 = mod.run_identity_sentence_refresh(max_per_run=5)
    assert result2["undefinable"] == 0
    session = get_session()
    try:
        count = (session.query(KGMaintenanceFinding)
                 .filter_by(finding_type="undefinable_node", primary_node_id=nid)
                 .count())
        assert count == 1
    finally:
        session.close()


# ── semantic tier reads the identity collection ──────────────────────────


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows
        self.queried_with = None

    def query(self, *, query_embeddings, n_results, include):
        self.queried_with = query_embeddings[0]
        return {
            "ids": [[cid for cid, _ in self._rows[:n_results]]],
            "embeddings": [[emb for _, emb in self._rows[:n_results]]],
        }


def test_semantic_tier_finds_candidates_via_identity_collection(monkeypatch):
    from app.assistant.kg.proposal_promoter import _semantic_entity_candidates

    target = _mk_node("Jo's House", node_type="Entity")

    label_vec = [1.0, 0.0, 0.0]
    sentence_vec = [0.0, 1.0, 0.0]
    embeds = {"House": label_vec, "Jo and Sam's house is in Springfield.": sentence_vec}

    import app.assistant.embeddings.embedder as embedder_mod
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(embedder_mod, "embed_text",
                        lambda text: embeds.get(text, [0.0, 0.0, 1.0]))

    identity_col = _FakeCollection([(target, [0.0, 0.98, 0.02])])  # near sentence_vec
    fake_cm = SimpleNamespace(
        node_collection=_FakeCollection([]),
        node_context_collection=_FakeCollection([]),
        node_identity_collection=identity_col,
    )
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake_cm)

    session = get_session()
    try:
        cands = _semantic_entity_candidates(
            session, "House", "Entity",
            sentence="Jo and Sam's house is in Springfield.",
        )
    finally:
        session.close()

    assert {c["node_id"] for c in cands} == {target}
    # The identity collection must be queried with the SENTENCE embedding,
    # not the label embedding — like-vs-like comparison.
    assert identity_col.queried_with == sentence_vec


def test_candidate_snapshot_carries_identity_sentence():
    from app.assistant.kg.proposal_promoter import _snapshot_state_candidate

    nid = _mk_node("Snap Node")
    session = get_session()
    try:
        n = session.get(Node, nid)
        n.identity_sentence = "The snap node."
        session.commit()
        snap = _snapshot_state_candidate(session, n)
    finally:
        session.close()
    assert snap["identity_sentence"] == "The snap node."
