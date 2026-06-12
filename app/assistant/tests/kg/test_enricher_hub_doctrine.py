"""Hub doctrine for the context enricher (fragility review #6, move 3).

Above _HUB_EDGE_THRESHOLD edges, a 5-per-direction edge sample is
near-random — the enricher renders the entity CARD instead. No card →
keeps the raw sample with an honest hub note (sink, not drop).

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.assistant.control_nodes.context_enricher_prep_node as prep
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.models.base import get_session


def _mk_node(label, **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Entity", **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edges(nid, n):
    session = get_session()
    try:
        for i in range(n):
            other = Node(id=str(uuid.uuid4()), label=f"N{i}", node_type="Entity")
            session.add(other)
            session.flush()
            session.add(Edge(id=str(uuid.uuid4()), source_id=nid,
                             target_id=other.id, relationship_type="relates_to"))
        session.commit()
    finally:
        session.close()


def _build_context(monkeypatch, node_id, card_text):
    import app.assistant.kg_core.kg_utils.kg_tools as kg_tools
    import app.assistant.entity_management.entity_card_v2 as card_mod

    session = get_session()
    try:
        node = session.get(Node, node_id)
        monkeypatch.setattr(
            kg_tools, "semantic_find_node_by_text",
            lambda term, s, threshold=0.4, k=8: [(node, 0.99)],
        )
        monkeypatch.setattr(
            card_mod, "render_v2_card_for_prompt_injection_level",
            lambda s, name, level=0, sections=None: card_text,
        )
        return prep._build_kg_context(["anything"], session)
    finally:
        session.close()


def test_hub_node_renders_card_not_edge_sample(monkeypatch):
    hub = _mk_node("Hub Person")
    _mk_edges(hub, prep._HUB_EDGE_THRESHOLD + 5)

    out = _build_context(monkeypatch, hub, "## Hub Person\n- curated fact one")
    assert "curated card instead" in out
    assert "curated fact one" in out
    assert "relates_to" not in out  # raw edges suppressed


def test_hub_node_without_card_keeps_sample_with_note(monkeypatch):
    hub = _mk_node("Cardless Hub")
    _mk_edges(hub, prep._HUB_EDGE_THRESHOLD + 5)

    out = _build_context(monkeypatch, hub, "")
    assert "no card" in out
    assert "relates_to" in out  # sink, not drop


def test_sparse_node_keeps_raw_edges(monkeypatch):
    sparse = _mk_node("Sparse Node")
    _mk_edges(sparse, 3)

    out = _build_context(monkeypatch, sparse, "should not appear")
    assert "relates_to" in out
    assert "should not appear" not in out
    assert "hub node" not in out
