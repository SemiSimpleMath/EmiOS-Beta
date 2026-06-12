"""KG embedding index freshness (fragility review #4, 2026-06-12).

The reconciler (hourly diff / nightly full) and the ORM-event chokepoint:
no Node mutation path can forget chroma, drift cannot survive an hour,
and a full rebuild is the nightly floor.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid

import pytest

import app.assistant.kg.chroma.embedding_sync as sync_mod
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.models.base import get_session


def _mk_node(label, identity_sentence=None, **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Entity",
                         identity_sentence=identity_sentence, **fields))
        session.commit()
    finally:
        session.close()
    return nid


class _FakeCollection:
    def __init__(self, rows=None):
        # rows: {node_id: metadata}
        self.rows = dict(rows or {})

    def get(self, *args, **kwargs):
        ids = list(self.rows)
        return {"ids": ids, "metadatas": [self.rows[i] for i in ids]}


class _FakeChroma:
    def __init__(self, labels=None, identities=None, contexts=None):
        self.node_collection = _FakeCollection(labels)
        self.node_identity_collection = _FakeCollection(identities)
        self.node_context_collection = _FakeCollection(contexts)
        self.stored_labels = {}
        self.stored_identities = {}
        self.deleted = []

    def store_node_embedding(self, nid, label, emb):
        self.stored_labels[nid] = label

    def store_node_identity_embedding(self, nid, sentence, emb):
        self.stored_identities[nid] = sentence

    def delete_node_embedding(self, nid):
        self.deleted.append(("label", nid))

    def delete_node_context_embedding(self, nid):
        self.deleted.append(("context", nid))

    def delete_node_identity_embedding(self, nid):
        self.deleted.append(("identity", nid))


@pytest.fixture
def fake_embed(monkeypatch):
    import app.assistant.embeddings.embedder as embedder_mod
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: [0.5])


@pytest.fixture
def status_to_tmp(monkeypatch):
    # Capture the status payload instead of redirecting get_data_dir —
    # the DB path derives from the data dir, so patching it reroutes the
    # whole database out from under the test.
    captured = {}
    monkeypatch.setattr(sync_mod, "_write_status",
                        lambda counts: captured.update(counts))
    return captured


# ── reconciler ────────────────────────────────────────────────────────────


def test_diff_sync_heals_ghosts_missing_and_stale(monkeypatch, fake_embed, status_to_tmp):
    fresh = _mk_node("Fresh Node")
    stale = _mk_node("Renamed Node")
    with_identity = _mk_node("Sentenced", identity_sentence="The sentenced node.")
    dropped_identity = _mk_node("Unsentenced")  # vector exists, sentence gone

    ghost_id = str(uuid.uuid4())  # in chroma, not in sqlite
    fake = _FakeChroma(
        labels={
            fresh: {"label": "Fresh Node"},
            stale: {"label": "Old Name"},          # text drift
            ghost_id: {"label": "Deleted Node"},   # ghost
            with_identity: {"label": "Sentenced"},
            dropped_identity: {"label": "Unsentenced"},
        },
        identities={
            dropped_identity: {"identity_sentence": "obsolete"},
        },
    )
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake)

    counts = sync_mod.sync_kg_embeddings(mode="diff")

    assert counts["ghosts_removed"] == 1
    assert ("label", ghost_id) in fake.deleted
    # stale label re-embedded; fresh untouched
    assert stale in fake.stored_labels and fresh not in fake.stored_labels
    # missing identity vector embedded; orphaned identity vector removed
    assert fake.stored_identities[with_identity] == "The sentenced node."
    assert ("identity", dropped_identity) in fake.deleted
    # status payload recorded
    assert status_to_tmp.get("mode") == "diff"


def test_full_sync_reembeds_everything(monkeypatch, fake_embed, status_to_tmp):
    a = _mk_node("A", identity_sentence="The A node.")
    b = _mk_node("B")
    fake = _FakeChroma(
        labels={a: {"label": "A"}, b: {"label": "B"}},
        identities={a: {"identity_sentence": "The A node."}},
    )
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake)

    counts = sync_mod.sync_kg_embeddings(mode="full")
    assert set(fake.stored_labels) == {a, b}
    assert set(fake.stored_identities) == {a}
    assert counts["labels_embedded"] == 2


# ── ORM chokepoint ───────────────────────────────────────────────────────


@pytest.fixture
def chokepoint(monkeypatch, fake_embed):
    fake = _FakeChroma()
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake)
    sync_mod.register_kg_embedding_sync()
    yield fake
    sync_mod.unregister_kg_embedding_sync()


def test_chokepoint_embeds_on_insert_and_label_change(chokepoint):
    nid = _mk_node("Chokepoint Node")  # ORM insert + commit
    assert chokepoint.stored_labels.get(nid) == "Chokepoint Node"

    session = get_session()
    try:
        session.get(Node, nid).label = "Renamed Chokepoint"
        session.commit()
    finally:
        session.close()
    assert chokepoint.stored_labels.get(nid) == "Renamed Chokepoint"


def test_chokepoint_skips_irrelevant_updates(chokepoint):
    nid = _mk_node("Quiet Node")
    chokepoint.stored_labels.clear()

    session = get_session()
    try:
        session.get(Node, nid).importance = 5.0  # not identity text
        session.commit()
    finally:
        session.close()
    assert nid not in chokepoint.stored_labels


def test_chokepoint_deletes_vectors_on_node_delete(chokepoint):
    nid = _mk_node("Doomed Node")
    session = get_session()
    try:
        session.delete(session.get(Node, nid))
        session.commit()
    finally:
        session.close()
    assert ("label", nid) in chokepoint.deleted
    assert ("identity", nid) in chokepoint.deleted
