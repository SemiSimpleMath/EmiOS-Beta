"""A Disambiguation marker retires when its label is back to one referent (2026-10-05).

On 2026-06-27 an LLM outage minted markers for the owner and two family members; the twins were later merged, but
nothing ever removed a marker, so every new mention kept parking on it. Pins: with exactly one same-label node, every
parked edge moves to it (a duplicate hands its evidence to the existing edge and is deleted), the marker's evidence is
rebound and the marker is deleted, each step in the revision log; two referents keep the marker; a locked node on a
parked edge's other end keeps that edge, and so the marker.

Uses the kg conftest (isolated test DB, fresh tables per test). Invented data only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.assistant.database.kg_chat_projection import KGEdgeEvidence, KGNodeEvidence
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE
from app.assistant.pipelines.kg_maintenance_pipeline.step_disambiguation_scan import retire_single_referent
from app.models.base import get_session

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def _node(label, node_type="Entity", locked=False):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type=node_type, locked_by_user_at=NOW if locked else None))
        s.commit()
    finally:
        s.close()
    return nid


def _edge(src, tgt, rel="has_state"):
    eid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Edge(id=eid, source_id=src, target_id=tgt, relationship_type=rel, sentence=rel))
        s.add(KGEdgeEvidence(edge_id=eid, source_table="unified_log_2026", message_timestamp=NOW))
        s.commit()
    finally:
        s.close()
    return eid


def _retire(dis, label):
    s = get_session()
    try:
        out = retire_single_referent(s, dis, label)
        s.commit()
        return out
    finally:
        s.close()


def test_a_marker_with_one_referent_moves_every_edge_and_retires():
    owner = _node("Sam", locked=True)
    dis = _node("Sam", node_type=DISAMBIGUATION_NODE_TYPE)
    trip, hotel = _node("Trip", "State"), _node("Hotel Booking", "State")
    moved = _edge(dis, trip)
    _edge(owner, hotel)
    dup = _edge(dis, hotel)
    s = get_session()
    try:
        s.add(KGNodeEvidence(node_id=dis, source_table="unified_log_2026", message_timestamp=NOW))
        s.commit()
    finally:
        s.close()

    out = _retire(dis, "Sam")
    assert out["deleted"] and out["repointed"] == [moved] and out["deduplicated"] == [dup]
    s = get_session()
    try:
        assert s.get(Node, dis) is None and s.get(Edge, moved).source_id == owner and s.get(Edge, dup) is None
        kept = s.query(Edge).filter(Edge.source_id == owner, Edge.target_id == hotel).one()
        assert s.query(KGEdgeEvidence).filter(KGEdgeEvidence.edge_id == kept.id).count() == 2, "the duplicate's evidence"
        assert s.query(KGNodeEvidence).filter(KGNodeEvidence.node_id == owner).count() == 1
        ops = sorted(r.op for r in s.query(KGRevisionLog).filter(
            KGRevisionLog.agent_id == "disambiguation_scan_retire").all())
        assert ops == ["delete_edge", "delete_node", "repoint_edge"]
    finally:
        s.close()


def test_two_referents_keep_the_marker():
    _node("Alex"), _node("Alex")
    dis = _node("Alex", node_type=DISAMBIGUATION_NODE_TYPE)
    eid = _edge(dis, _node("Job", "State"))
    out = _retire(dis, "Alex")
    assert out == {"referent": None, "repointed": [], "deduplicated": [], "left_parked": 0, "deleted": False}
    s = get_session()
    try:
        assert s.get(Node, dis) is not None and s.get(Edge, eid).source_id == dis
    finally:
        s.close()


def test_a_locked_node_on_the_other_end_keeps_its_edge_and_the_marker():
    _node("Pat")
    dis = _node("Pat", node_type=DISAMBIGUATION_NODE_TYPE)
    locked_state = _node("Locked Fact", "State", locked=True)
    eid = _edge(dis, locked_state)
    out = _retire(dis, "Pat")
    assert out["left_parked"] == 1 and not out["deleted"]
    s = get_session()
    try:
        assert s.get(Node, dis) is not None and s.get(Edge, eid).source_id == dis
    finally:
        s.close()
