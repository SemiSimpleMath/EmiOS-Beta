"""Deletion lifecycle (2026-07-08 KG audit G1).

The Edge FK CASCADE was dropped 2026-05-10 and the evidence tables have no
FKs at all, so nothing cascades on delete. These pin the explicit lifecycle:
delete_node removes the node's edges, both evidence families, and supersedes
verdicts; delete_edge removes edge evidence; reroute_edges cleans the
evidence of dropped duplicate edges; and the lifecycle_gc step sweeps any
historical residue.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid

from sqlalchemy import text as sql_text

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.kg_core.kg_utils.kg_tools import delete_edge, delete_node
from app.assistant.kg_core.kg_utils.node_merge import reroute_edges
from app.models.base import get_session


def _mk_node(session, label: str) -> Node:
    n = Node(id=str(uuid.uuid4()), label=label, node_type="Entity", attributes={})
    session.add(n)
    session.flush()
    return n


def _mk_edge(session, src: str, tgt: str, rel: str = "relates_to") -> Edge:
    e = Edge(id=str(uuid.uuid4()), source_id=src, target_id=tgt,
             relationship_type=rel, attributes={})
    session.add(e)
    session.flush()
    return e


def _add_node_evidence(session, node_id: str) -> None:
    session.execute(sql_text(
        "INSERT INTO kg_node_evidence (id, node_id, merge_action, created_at) "
        "VALUES (:id, :nid, 'created', :now)"
    ), {"id": str(uuid.uuid4()), "nid": node_id, "now": "2026-07-08T00:00:00+00:00"})


def _add_edge_evidence(session, edge_id: str) -> None:
    session.execute(sql_text(
        "INSERT INTO kg_edge_evidence (id, edge_id, merge_action, created_at) "
        "VALUES (:id, :eid, 'created', :now)"
    ), {"id": str(uuid.uuid4()), "eid": edge_id, "now": "2026-07-08T00:00:00+00:00"})


def _count(session, table: str, col: str, val: str) -> int:
    return session.execute(
        sql_text(f"SELECT COUNT(*) FROM {table} WHERE {col} = :v"), {"v": val}
    ).scalar()


def test_delete_node_cleans_edges_evidence_and_verdicts():
    session = get_session()
    try:
        a = _mk_node(session, "Alpha Test Node")
        b = _mk_node(session, "Beta Test Node")
        e = _mk_edge(session, a.id, b.id)
        a_id, e_id = a.id, e.id
        _add_node_evidence(session, a_id)
        _add_edge_evidence(session, e_id)
        session.add(KGNodeVerdict(
            id=str(uuid.uuid4()), node_id_a=a_id, node_id_b=b.id,
            verdict_type="distinct", memo="test pair", decided_by="test",
        ))
        session.flush()

        delete_node(a_id, session)
        session.commit()

        assert _count(session, "kg_node_metadata", "id", a_id) == 0
        assert _count(session, "kg_edge_metadata", "source_id", a_id) == 0
        assert _count(session, "kg_node_evidence", "node_id", a_id) == 0
        assert _count(session, "kg_edge_evidence", "edge_id", e_id) == 0
        active = session.execute(sql_text(
            "SELECT COUNT(*) FROM kg_node_verdict "
            "WHERE node_id_a = :a AND superseded_at IS NULL"
        ), {"a": a_id}).scalar()
        assert active == 0
    finally:
        session.rollback()
        session.close()


def test_delete_edge_cleans_evidence():
    session = get_session()
    try:
        a = _mk_node(session, "Gamma Test Node")
        b = _mk_node(session, "Delta Test Node")
        e = _mk_edge(session, a.id, b.id)
        _add_edge_evidence(session, e.id)
        session.flush()

        assert delete_edge(e.id, session) is True
        session.commit()

        assert _count(session, "kg_edge_metadata", "id", e.id) == 0
        assert _count(session, "kg_edge_evidence", "edge_id", e.id) == 0
    finally:
        session.rollback()
        session.close()


def test_reroute_drops_duplicate_edge_with_its_evidence():
    session = get_session()
    try:
        winner = _mk_node(session, "Winner Test Node")
        loser = _mk_node(session, "Loser Test Node")
        other = _mk_node(session, "Other Test Node")
        # Winner already has the edge; loser's duplicate must be dropped.
        _mk_edge(session, winner.id, other.id, "relates_to")
        dup = _mk_edge(session, loser.id, other.id, "relates_to")
        dup_id = dup.id
        _add_edge_evidence(session, dup_id)
        session.flush()

        rerouted, dropped = reroute_edges(session, loser.id, winner.id)
        session.commit()

        assert [d["id"] for d in dropped] == [dup_id]
        assert _count(session, "kg_edge_evidence", "edge_id", dup_id) == 0
    finally:
        session.rollback()
        session.close()


def test_lifecycle_gc_sweeps_residue():
    from app.assistant.pipelines.kg_maintenance_pipeline.step_lifecycle_gc import run

    session = get_session()
    try:
        live = _mk_node(session, "Live Test Node")
        dead_id = str(uuid.uuid4())  # never inserted — simulates a deleted node
        dead_edge_id = str(uuid.uuid4())
        _add_node_evidence(session, dead_id)
        _add_edge_evidence(session, dead_edge_id)
        session.add(KGNodeVerdict(
            id=str(uuid.uuid4()), node_id_a=live.id, node_id_b=dead_id,
            verdict_type="distinct", memo="test pair", decided_by="test",
        ))
        session.commit()
    finally:
        session.close()

    counts = run()

    assert counts["node_evidence_orphans_deleted"] >= 1
    assert counts["edge_evidence_orphans_deleted"] >= 1
    assert counts["dead_verdicts_superseded"] >= 1

    # Second run: steady state, nothing left to sweep.
    again = run()
    assert again["node_evidence_orphans_deleted"] == 0
    assert again["edge_evidence_orphans_deleted"] == 0
    assert again["dead_verdicts_superseded"] == 0


def test_unparseable_dates_route_to_prose_not_none():
    """Audit G3: partial dates the extractor emits ("2024-08") must survive
    as prose, not vanish; real ISO stamps still parse; explicit prose from
    the extractor wins over the leftover."""
    from app.assistant.kg.proposal_writer import _parse_ts_or_prose

    parsed, leftover = _parse_ts_or_prose("2026-07-08T12:00:00+00:00")
    assert parsed is not None and leftover is None

    parsed, leftover = _parse_ts_or_prose("spring 2023")
    assert parsed is None and leftover == "spring 2023"

    parsed, leftover = _parse_ts_or_prose(None)
    assert parsed is None and leftover is None

    parsed, leftover = _parse_ts_or_prose("   ")
    assert parsed is None and leftover is None
