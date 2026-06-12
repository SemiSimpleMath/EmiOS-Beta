"""Disambiguation attach-here semantics (rework 2026-06-10).

A Disambiguation node is an ATTACHMENT POINT, not a forward: when a
label's referent is ambiguous, mentions bind to the Disambiguation node
itself (edges land on it), and the maintenance loop later re-points
each edge to its true referent via kg_repoint_edge. Covers:

- resolver: a Disambiguation at the label wins resolution (any
  entity-like type, even over an exact same-type label hit)
- promoter: a create that collides with an existing same-type label
  mints a Disambiguation at that label (and only then)
- series_link executor: leaves aliases on the parent Entity instead of
  minting Disambiguation markers
- kg_repoint_edge: moves one endpoint in place (id/sentence preserved),
  audit-logged, refuses locked rows / duplicates / no-ops
- disambiguation_scan: raises disambiguation_backlog only for
  Disambiguation nodes carrying edges; orphan_scan ignores edge-less
  Disambiguation nodes (resting state)

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.disambiguation import (
    DISAMBIGUATION_NODE_TYPE,
    create_disambiguation,
    find_disambiguation,
)
from app.assistant.kg.proposal_promoter import (
    _mint_disambiguation_on_label_collision,
    _resolve_entity_like,
)
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.utils.pydantic_classes import ToolMessage
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_revision_log():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_node(label: str, node_type: str = "Entity", locked: bool = False, **fields) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(
            id=nid, label=label, node_type=node_type,
            locked_by_user_at=NOW if locked else None, **fields,
        ))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id: str, target_id: str, rel: str = "related_to",
             sentence: str = "", locked: bool = False) -> str:
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(
            id=eid, source_id=source_id, target_id=target_id,
            relationship_type=rel, sentence=sentence or None,
            locked_by_user_at=NOW if locked else None,
        ))
        session.commit()
    finally:
        session.close()
    return eid


def _call(tool_name: str, **arguments):
    msg = ToolMessage(tool_name=tool_name, tool_data={"tool_name": tool_name, "arguments": arguments})
    return KGMutatorTool().execute(msg)


def _is_refusal(result, *needles: str) -> bool:
    content = (result.content or "").lower()
    return all(n.lower() in content for n in needles)


def _revision_rows(op: str) -> list:
    session = get_session()
    try:
        return session.query(KGRevisionLog).filter(KGRevisionLog.op == op).all()
    finally:
        session.close()


def _disambiguations_at(label: str) -> list:
    session = get_session()
    try:
        return (
            session.query(Node)
            .filter(Node.node_type == DISAMBIGUATION_NODE_TYPE)
            .filter(Node.label == label)
            .all()
        )
    finally:
        session.close()


# ── resolver: Disambiguation wins resolution ─────────────────────────────


def test_resolver_binds_to_disambiguation_over_exact_label_hit():
    _mk_node("Alex", category="person")
    session = get_session()
    try:
        dis = create_disambiguation(session, label="Alex", reason="two Alexes")
        session.commit()
        dis_id = str(dis.id)
    finally:
        session.close()

    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "Alex", "Entity")
        assert str(hit.id) == dis_id and tier == "disambiguation"
        # Any entity-like type binds to the attachment point (type-gate bypass).
        hit2, tier2 = _resolve_entity_like(session, "Alex", "Concept")
        assert str(hit2.id) == dis_id and tier2 == "disambiguation"
    finally:
        session.close()


def test_resolver_unaffected_when_no_disambiguation():
    nid = _mk_node("Springfield")
    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "Springfield", "Entity")
        assert str(hit.id) == nid and tier == "label"
    finally:
        session.close()


# ── promoter: mint on exact-label collision ──────────────────────────────


def test_collision_mints_disambiguation():
    _mk_node("Alex", category="person")
    twin_id = _mk_node("Alex", category="person")  # the declined-match create
    session = get_session()
    try:
        twin = session.get(Node, twin_id)
        _mint_disambiguation_on_label_collision(session, twin)
        session.commit()
    finally:
        session.close()
    assert len(_disambiguations_at("Alex")) == 1


def test_no_collision_no_mint():
    nid = _mk_node("Unique Label")
    session = get_session()
    try:
        node = session.get(Node, nid)
        _mint_disambiguation_on_label_collision(session, node)
        session.commit()
    finally:
        session.close()
    assert _disambiguations_at("Unique Label") == []


def test_cross_type_label_share_does_not_mint():
    _mk_node("Fitness", node_type="Concept")
    goal_id = _mk_node("Fitness", node_type="Goal")
    session = get_session()
    try:
        goal = session.get(Node, goal_id)
        _mint_disambiguation_on_label_collision(session, goal)
        session.commit()
    finally:
        session.close()
    # Same label across different types is the type gate's business, not
    # referent ambiguity.
    assert _disambiguations_at("Fitness") == []


def test_mint_is_idempotent():
    _mk_node("Alex")
    twin_id = _mk_node("Alex")
    session = get_session()
    try:
        twin = session.get(Node, twin_id)
        _mint_disambiguation_on_label_collision(session, twin)
        _mint_disambiguation_on_label_collision(session, twin)
        session.commit()
    finally:
        session.close()
    assert len(_disambiguations_at("Alex")) == 1


# ── disambiguation module basics ─────────────────────────────────────────


def test_create_disambiguation_idempotent_and_findable():
    session = get_session()
    try:
        a = create_disambiguation(session, label="Alex's Sister", reason="many sisters")
        b = create_disambiguation(session, label="alex's sister")
        session.commit()
        assert str(a.id) == str(b.id)
        found = find_disambiguation(session, "ALEX'S SISTER")
        assert found is not None and str(found.id) == str(a.id)
    finally:
        session.close()


# ── series_link executor: aliases, not markers ───────────────────────────


def test_series_link_adds_aliases_instead_of_disambiguation():
    from app.assistant.pipelines.kg_maintenance_pipeline.step_execute_findings import (
        _execute_series_link,
    )
    event_id = _mk_node("Friday Night Meats", node_type="Event")
    finding = {
        "primary_node_id": event_id,
        "secondary_node_id": event_id,
        "evidence_json": {
            "action": "create_parent_entity_and_link",
            "canonical_label": "Friday Night Meats gatherings",
            "all_event_ids": [event_id],
        },
        "investigation_report_json": {},
    }
    result = _execute_series_link(finding)
    assert result["executed"] is True, result

    session = get_session()
    try:
        parent = (
            session.query(Node)
            .filter(Node.label == "Friday Night Meats gatherings")
            .filter(Node.node_type == "Entity")
            .one()
        )
        # The event's label became an alias on the parent; no marker node.
        assert "Friday Night Meats" in (parent.aliases or [])
        dis_count = (
            session.query(Node)
            .filter(Node.node_type == DISAMBIGUATION_NODE_TYPE)
            .count()
        )
        assert dis_count == 0
        edge = (
            session.query(Edge)
            .filter(Edge.source_id == event_id)
            .filter(Edge.target_id == parent.id)
            .filter(Edge.relationship_type == "instance_of")
            .one_or_none()
        )
        assert edge is not None
    finally:
        session.close()


# ── kg_repoint_edge ──────────────────────────────────────────────────────


def test_repoint_edge_moves_endpoint_in_place_and_logs():
    dis = _mk_node("Alex's Sister", node_type=DISAMBIGUATION_NODE_TYPE)
    event = _mk_node("Visit", node_type="Event")
    mary = _mk_node("Mary", category="person")
    eid = _mk_edge(event, dis, "participant", sentence="Alex's sister visited.")

    result = _call(
        "kg_repoint_edge", edge_id=eid, new_target_id=mary,
        reason="referent identified as Mary",
    )
    assert result.data.get("ok") is True, result.content

    session = get_session()
    try:
        edge = session.get(Edge, eid)
        assert edge is not None  # same row, not delete+create
        assert edge.source_id == event
        assert edge.target_id == mary
        assert edge.sentence == "Alex's sister visited."
    finally:
        session.close()

    rows = _revision_rows("repoint_edge")
    assert len(rows) == 1
    assert rows[0].before_json["target_id"] == dis
    assert rows[0].after_json["target_id"] == mary
    assert rows[0].reason == "referent identified as Mary"


def test_repoint_edge_dry_run_changes_nothing():
    a, b, c = _mk_node("A"), _mk_node("B"), _mk_node("C")
    eid = _mk_edge(a, b)
    result = _call("kg_repoint_edge", edge_id=eid, new_target_id=c,
                   reason="preview", dry_run=True)
    assert result.data.get("dry_run") is True
    session = get_session()
    try:
        assert session.get(Edge, eid).target_id == b
    finally:
        session.close()
    assert _revision_rows("repoint_edge") == []


def test_repoint_edge_refusals():
    a, b, c = _mk_node("A"), _mk_node("B"), _mk_node("C")
    locked_node = _mk_node("Locked", locked=True)
    eid = _mk_edge(a, b)
    locked_eid = _mk_edge(a, c, rel="mentions", locked=True)

    # Locked edge.
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=locked_eid, new_target_id=b, reason="x"),
        "user-locked", "kg_repoint_edge",
    )
    # Locked NEW endpoint.
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, new_target_id=locked_node, reason="x"),
        "user-locked",
    )
    # Missing new endpoint node.
    missing = str(uuid.uuid4())
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, new_target_id=missing, reason="x"),
        "not found",
    )
    # No-op re-point.
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, new_target_id=b, reason="x"),
        "no-op",
    )
    # Self-loop.
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, new_target_id=a, reason="x"),
        "self-loop",
    )
    # Duplicate-creating re-point.
    _mk_edge(a, c, rel="related_to")
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, new_target_id=c, reason="x"),
        "already exists",
    )
    # Neither endpoint given.
    assert _is_refusal(
        _call("kg_repoint_edge", edge_id=eid, reason="x"),
        "new_source_id",
    )

    # Nothing moved, nothing logged.
    session = get_session()
    try:
        assert session.get(Edge, eid).target_id == b
    finally:
        session.close()
    assert _revision_rows("repoint_edge") == []


# ── scanners ─────────────────────────────────────────────────────────────


def test_disambiguation_scan_raises_only_for_backlogged_nodes():
    from app.assistant.pipelines.kg_maintenance_pipeline.step_disambiguation_scan import run

    resting = _mk_node("Alex's Sister", node_type=DISAMBIGUATION_NODE_TYPE)
    backlogged = _mk_node("Alex", node_type=DISAMBIGUATION_NODE_TYPE)
    event = _mk_node("Visit", node_type="Event")
    _mk_edge(event, backlogged, "participant")

    result = run(SimpleNamespace(run_id="test-run"))
    assert result["scanned"] == 2
    assert result["with_backlog"] == 1
    assert result["new_findings"] == 1

    session = get_session()
    try:
        findings = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "disambiguation_backlog")
            .all()
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.primary_node_id == backlogged
        assert f.suggested_action == "repoint_edges"
        assert f.evidence_json["edge_count"] == 1
    finally:
        session.close()

    # Re-run: dedup keeps it at one pending finding.
    result2 = run(SimpleNamespace(run_id="test-run-2"))
    assert result2["new_findings"] == 0
    assert resting  # silence linter — resting node intentionally unflagged


def test_orphan_scan_ignores_edgeless_disambiguation():
    from app.assistant.pipelines.kg_maintenance_pipeline.step_orphan_scan import run

    _mk_node("Alex's Sister", node_type=DISAMBIGUATION_NODE_TYPE)
    true_orphan = _mk_node("Stranded")

    run(SimpleNamespace(run_id="test-run"))

    session = get_session()
    try:
        flagged = {
            f.primary_node_id
            for f in session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "orphan_node")
            .all()
        }
    finally:
        session.close()
    assert true_orphan in flagged
    dis_ids = {str(n.id) for n in _disambiguations_at("Alex's Sister")}
    assert not (flagged & dis_ids)
