"""edge_canon_curation apply phase (revived + wired 2026-07-08, audit G2).

The LLM curator is not exercised here — these pin the deterministic apply:
variant_of inserts the alias, rewrites edges to canonical, and drops
colliding variants WITH their evidence; new_canon promotes a row; not_yet
is a no-op. Uses the kg conftest (isolated test DB).
"""
from __future__ import annotations

import uuid

from sqlalchemy import text as sql_text

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.pipelines.kg_maintenance_pipeline.step_edge_canon_curation import (
    _apply_verdicts,
)
from app.models.base import get_session


def _seed_graph(session):
    a = Node(id=str(uuid.uuid4()), label="Canon Src", node_type="Entity", attributes={})
    b = Node(id=str(uuid.uuid4()), label="Canon Tgt", node_type="Entity", attributes={})
    session.add_all([a, b])
    session.flush()
    return a, b


def _add_edge(session, src, tgt, rel):
    e = Edge(id=str(uuid.uuid4()), source_id=src, target_id=tgt,
             relationship_type=rel, attributes={})
    session.add(e)
    session.flush()
    return e.id


def _add_canon(session, edge_type):
    session.execute(sql_text(
        "INSERT INTO edge_canon (id, edge_type, domain_type, range_type, created_at) "
        "VALUES (:id, :et, 'Entity', 'Entity', '2026-07-08T00:00:00+00:00')"
    ), {"id": str(uuid.uuid4()), "et": edge_type})


def _add_edge_evidence(session, edge_id):
    session.execute(sql_text(
        "INSERT INTO kg_edge_evidence (id, edge_id, merge_action, created_at) "
        "VALUES (:id, :eid, 'created', '2026-07-08T00:00:00+00:00')"
    ), {"id": str(uuid.uuid4()), "eid": edge_id})


def test_variant_of_rewrites_and_drops_collisions_with_evidence():
    # Unique names: edge_canon persists across tests in the shared test DB
    # (other suites seed the common vocabulary).
    canon = f"curates_ct_{uuid.uuid4().hex[:8]}"
    variant = f"curated_by_ct_{uuid.uuid4().hex[:8]}"
    session = get_session()
    try:
        a, b = _seed_graph(session)
        _add_canon(session, canon)
        # The canonical edge already exists; the variant on the SAME pair
        # must be dropped (with its evidence); a variant on a different
        # pair must be rewritten.
        _add_edge(session, a.id, b.id, canon)
        colliding = _add_edge(session, a.id, b.id, variant)
        _add_edge_evidence(session, colliding)
        c = Node(id=str(uuid.uuid4()), label="Canon Other", node_type="Entity", attributes={})
        session.add(c)
        session.flush()
        rewritable = _add_edge(session, a.id, c.id, variant)
        session.commit()
    finally:
        session.close()

    stats = _apply_verdicts([
        {"predicate": variant, "verdict": "variant_of", "canonical_target": canon},
    ])

    assert stats["alias_added"] == 1
    assert stats["edges_rewritten"] == 1

    session = get_session()
    try:
        assert session.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_metadata WHERE relationship_type = :r"
        ), {"r": variant}).scalar() == 0
        assert session.execute(sql_text(
            "SELECT relationship_type FROM kg_edge_metadata WHERE id = :i"
        ), {"i": rewritable}).scalar() == canon
        # The colliding variant is gone WITH its evidence.
        assert session.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_metadata WHERE id = :i"), {"i": colliding}
        ).scalar() == 0
        assert session.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_evidence WHERE edge_id = :i"), {"i": colliding}
        ).scalar() == 0
        assert session.execute(sql_text(
            "SELECT COUNT(*) FROM edge_alias WHERE raw_text = :r"), {"r": variant}
        ).scalar() == 1
    finally:
        session.close()


def test_new_canon_promotes_and_not_yet_noop():
    name = f"mentors_ct_{uuid.uuid4().hex[:8]}"
    stats = _apply_verdicts([
        {"predicate": name, "verdict": "new_canon", "canonical_target": name,
         "reason": "distinct semantics"},
        {"predicate": "maybe_thing", "verdict": "not_yet"},
    ])
    assert stats["canon_added"] == 1

    session = get_session()
    try:
        assert session.execute(sql_text(
            "SELECT COUNT(*) FROM edge_canon WHERE edge_type = :n"), {"n": name}
        ).scalar() == 1
    finally:
        session.close()
