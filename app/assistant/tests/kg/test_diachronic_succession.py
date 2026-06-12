"""Diachronic disambiguation (2026-06-12 pass).

A role-reference entity ("X's School") names a role whose concrete
referent changes over time. Covers the three mechanical pieces:

- kg_split_succession: closes the old era, mints a same-label successor,
  mints the Disambiguation park point, chains succeeded_by, optionally
  repoints era-mismatched edges; audited + refusal cases
- resolver: after a split, mentions at the label park on the
  Disambiguation node (existing precedence — comes free)
- temporal drain: parked edges re-point by evidence date when exactly
  one era contains it; everything ambiguous sinks to the investigator
- role_succession_scan: deterministic candidate selection, judge-driven
  finding lifecycle (succession -> investigated/needs_user_review,
  stable -> rejected cooldown stamp)

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_chat_projection import KGEdgeEvidence, KGNodeEvidence
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.disambiguation import (
    DISAMBIGUATION_NODE_TYPE,
    create_disambiguation,
    find_disambiguation,
)
from app.assistant.kg.proposal_promoter import _resolve_entity_like
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.pipelines.kg_maintenance_pipeline.step_disambiguation_scan import (
    temporal_drain,
)
from app.assistant.utils.pydantic_classes import ToolMessage
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def _clean_tables():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        session.query(KGMaintenanceFinding).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_node(label, node_type="Entity", locked=False, **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type,
                         locked_by_user_at=NOW if locked else None, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id, target_id, rel="related_to", locked=False, evidence_date=None):
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(id=eid, source_id=source_id, target_id=target_id,
                         relationship_type=rel,
                         locked_by_user_at=NOW if locked else None))
        if evidence_date is not None:
            session.add(KGEdgeEvidence(id=str(uuid.uuid4()), edge_id=eid,
                                       message_timestamp=evidence_date))
        session.commit()
    finally:
        session.close()
    return eid


def _mk_node_evidence(node_id, sentence, date):
    session = get_session()
    try:
        session.add(KGNodeEvidence(id=str(uuid.uuid4()), node_id=node_id,
                                   derived_sentence=sentence,
                                   message_timestamp=date))
        session.commit()
    finally:
        session.close()


def _call(tool_name, **arguments):
    msg = ToolMessage(tool_name=tool_name,
                      tool_data={"tool_name": tool_name, "arguments": arguments})
    return KGMutatorTool().execute(msg)


def _node(nid):
    session = get_session()
    try:
        return session.get(Node, nid)
    finally:
        session.close()


def _edge(eid):
    session = get_session()
    try:
        return session.get(Edge, eid)
    finally:
        session.close()


# ── kg_split_succession ──────────────────────────────────────────────────


def test_split_closes_era_mints_successor_disambiguation_and_chain():
    school = _mk_node("Kid's School", start_date=D(2020, 9, 1),
                      start_date_confidence="estimated", category="school")
    kid = _mk_node("Kid")
    old_edge = _mk_edge(kid, school, rel="attends")
    new_era_edge = _mk_edge(kid, school, rel="dropped_off_at")

    r = _call("kg_split_succession", node_id=school, split_date="2026-09-01",
              reason="new school starting fall 2026",
              repoint_edge_ids=[new_era_edge])
    assert r.data["ok"] is True

    old = _node(school)
    assert old.end_date is not None and old.end_date.date().isoformat() == "2026-09-01"
    assert old.end_date_confidence == "estimated"

    successor = _node(r.data["successor_node_id"])
    assert successor.label == "Kid's School"
    assert successor.node_type == "Entity"
    assert successor.start_date.date().isoformat() == "2026-09-01"

    session = get_session()
    try:
        dis = find_disambiguation(session, "Kid's School")
        assert dis is not None and dis.id == r.data["disambiguation_node_id"]
        chain = session.get(Edge, r.data["succeeded_by_edge_id"])
        assert chain.source_id == school and chain.target_id == successor.id
        assert chain.relationship_type == "succeeded_by"
    finally:
        session.close()

    # Named era-mismatched edge moved; the other stayed.
    assert _edge(new_era_edge).target_id == successor.id
    assert _edge(old_edge).target_id == school
    assert r.data["repointed"] == [new_era_edge]

    session = get_session()
    try:
        rows = session.query(KGRevisionLog).filter_by(op="split_succession").all()
        assert len(rows) == 1
    finally:
        session.close()


def test_after_split_mentions_park_on_disambiguation():
    school = _mk_node("Kid's School", start_date=D(2020, 9, 1))
    _call("kg_split_succession", node_id=school, split_date="2026-09-01",
          reason="referent changed")
    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "Kid's School", "Entity")
        assert tier == "disambiguation"
        assert hit.node_type == DISAMBIGUATION_NODE_TYPE
    finally:
        session.close()


def test_split_refusals():
    locked = _mk_node("A's Car", locked=True)
    assert "user-locked" in (_call("kg_split_succession", node_id=locked,
                                   split_date="2026-01-01", reason="x").content or "")

    closed = _mk_node("B's Car", end_date=D(2025, 1, 1))
    assert "already closed" in (_call("kg_split_succession", node_id=closed,
                                      split_date="2026-01-01", reason="x").content or "")

    young = _mk_node("C's Car", start_date=D(2026, 5, 1))
    assert "postdate" in (_call("kg_split_succession", node_id=young,
                                split_date="2025-01-01", reason="x").content or "")

    state = _mk_node("Ownership", node_type="State")
    assert "kg_close_state" in (_call("kg_split_succession", node_id=state,
                                      split_date="2026-01-01", reason="x").content or "")

    # Edge not touching the node is a caller error.
    a, b = _mk_node("D's Car"), _mk_node("Unrelated")
    stray = _mk_edge(b, _mk_node("Other"))
    assert "does not touch" in (_call("kg_split_succession", node_id=a,
                                      split_date="2026-01-01", reason="x",
                                      repoint_edge_ids=[stray]).content or "")


def test_split_dry_run_mutates_nothing():
    school = _mk_node("Kid's School", start_date=D(2020, 9, 1))
    r = _call("kg_split_succession", node_id=school, split_date="2026-09-01",
              reason="preview", dry_run=True)
    assert r.data.get("dry_run") is True
    assert _node(school).end_date is None
    session = get_session()
    try:
        assert find_disambiguation(session, "Kid's School") is None
        assert session.query(Node).filter(Node.label == "Kid's School").count() == 1
    finally:
        session.close()


# ── temporal drain ───────────────────────────────────────────────────────


def _split_label_setup():
    """Two eras of 'Kid's School' (2020-09→2024-09, 2024-09→open) + park point."""
    old = _mk_node("Kid's School", start_date=D(2020, 9, 1), end_date=D(2024, 9, 1))
    new = _mk_node("Kid's School", start_date=D(2024, 9, 1))
    session = get_session()
    try:
        dis = create_disambiguation(session, label="Kid's School", reason="eras")
        session.commit()
        dis_id = str(dis.id)
    finally:
        session.close()
    return old, new, dis_id


def test_temporal_drain_repoints_by_evidence_date():
    old, new, dis_id = _split_label_setup()
    kid = _mk_node("Kid")
    e_old = _mk_edge(kid, dis_id, rel="dropped_off_at", evidence_date=D(2023, 5, 1))
    e_new = _mk_edge(kid, dis_id, rel="picked_up_at", evidence_date=D(2025, 5, 1))
    e_undatable = _mk_edge(kid, dis_id, rel="visited", evidence_date=D(2019, 1, 1))

    session = get_session()
    try:
        result = temporal_drain(session, dis_id, "Kid's School")
        session.commit()
    finally:
        session.close()

    assert set(result["repointed"]) == {e_old, e_new}
    assert _edge(e_old).target_id == old      # 2023 → old era
    assert _edge(e_new).target_id == new      # 2025 → new era
    assert _edge(e_undatable).target_id == dis_id  # predates all eras — sinks


def test_temporal_drain_sinks_locked_and_duplicate_edges():
    old, new, dis_id = _split_label_setup()
    kid = _mk_node("Kid")
    e_locked = _mk_edge(kid, dis_id, rel="attends", locked=True,
                        evidence_date=D(2025, 5, 1))
    _mk_edge(kid, new, rel="walks_to")  # pre-existing — makes repoint a dup
    e_dup = _mk_edge(kid, dis_id, rel="walks_to", evidence_date=D(2025, 5, 1))

    session = get_session()
    try:
        result = temporal_drain(session, dis_id, "Kid's School")
        session.commit()
    finally:
        session.close()

    assert result["repointed"] == []
    assert _edge(e_locked).target_id == dis_id
    assert _edge(e_dup).target_id == dis_id


def test_temporal_drain_requires_two_eras():
    # Synchronic ambiguity (one or zero same-label real nodes) is the
    # investigator's job — the date rule must not fire.
    only = _mk_node("Alex")
    session = get_session()
    try:
        dis = create_disambiguation(session, label="Alex", reason="two Alexes")
        session.commit()
        dis_id = str(dis.id)
    finally:
        session.close()
    e = _mk_edge(_mk_node("Friend"), dis_id, rel="knows", evidence_date=D(2025, 1, 1))

    session = get_session()
    try:
        result = temporal_drain(session, dis_id, "Alex")
        session.commit()
    finally:
        session.close()
    assert result["repointed"] == []
    assert _edge(e).target_id == dis_id


# ── role_succession_scan ─────────────────────────────────────────────────


def _scan(monkeypatch, verdicts):
    """Run the scan with the judge mocked; verdicts keyed by node label."""
    import app.assistant.pipelines.kg_maintenance_pipeline.step_role_succession_scan as mod

    calls = []

    def fake_judge(agent_input):
        calls.append(agent_input["label"])
        return verdicts.get(agent_input["label"])

    monkeypatch.setattr(mod, "_run_judge", fake_judge)
    result = mod.run(SimpleNamespace(run_id="test-run"))
    return result, calls


def _findings(finding_type="referent_succession"):
    session = get_session()
    try:
        return session.query(KGMaintenanceFinding).filter_by(
            finding_type=finding_type).all()
    finally:
        session.close()


def test_scan_selects_possessive_entities_and_routes_verdicts(monkeypatch):
    school = _mk_node("Kid's School", category="school")
    for i in range(4):
        _mk_node_evidence(school, f"school fact {i}", D(2024 + (i % 2), 3, 1))
    car = _mk_node("Kid's Car")
    for i in range(3):
        _mk_node_evidence(car, f"car fact {i}", D(2025, 1, 1))
    _mk_node("Plain Entity")                      # not possessive — skipped
    thin = _mk_node("Kid's Phone")                # < MIN_EVIDENCE — skipped
    _mk_node_evidence(thin, "one fact", D(2025, 1, 1))

    result, calls = _scan(monkeypatch, {
        "Kid's School": {
            "verdict": "succession", "confidence": "high",
            "split_date": "2026-09-01", "split_basis": "new school mentioned",
            "era_before": "old school", "era_after": "new school",
            "reasoning": "clear change",
        },
        "Kid's Car": {
            "verdict": "stable", "confidence": "medium",
            "reasoning": "one car throughout",
        },
    })

    assert set(calls) == {"Kid's School", "Kid's Car"}
    assert result["succession_findings"] == 1 and result["stable"] == 1

    by_node = {f.primary_node_id: f for f in _findings()}
    sf = by_node[school]
    assert sf.status == "investigated"
    report = dict(sf.investigation_report_json or {})
    assert report.get("disposition") == "needs_user_review"
    assert "kg_split_succession" in (report.get("recommendation") or "")
    assert by_node[car].status == "rejected"      # cooldown stamp


def test_scan_skips_cooldown_and_already_split_labels(monkeypatch):
    # Rejected stamp within the cooldown window → not re-judged.
    stamped = _mk_node("Kid's Desk")
    for i in range(3):
        _mk_node_evidence(stamped, f"fact {i}", D(2025, 1, 1))
    session = get_session()
    try:
        session.add(KGMaintenanceFinding(
            id=str(uuid.uuid4()), finding_type="referent_succession",
            primary_node_id=stamped, suggested_action="none",
            status="rejected", agent_name="role_succession_scan",
        ))
        # Already-split label → Disambiguation exists → skipped.
        create_disambiguation(session, label="Kid's Bike", reason="eras")
        session.commit()
    finally:
        session.close()
    bike = _mk_node("Kid's Bike")
    for i in range(3):
        _mk_node_evidence(bike, f"fact {i}", D(2025, 1, 1))

    result, calls = _scan(monkeypatch, {})
    assert calls == []
    assert result["judged"] == 0
