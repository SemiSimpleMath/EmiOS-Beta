"""Missing-dates scanner curation (2026-06-12).

Worth = max importance among connected non-primary entities (own
importance only when the primary user is the sole entity). Below the
floor → no finding, and pre-existing pending findings get swept to
'rejected'. Nobody wants to date-stamp the swivel chair.
"""
from __future__ import annotations

import uuid

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.importance.consumers import date_gap_priority
from app.assistant.pipelines.kg_maintenance_pipeline.step_missing_dates_scan import (
    WORTH_FLOOR,
    _worth,
    run,
)
from app.models.base import get_session


class _Ctx:
    run_id = "test-run"


def _mk_node(label: str, node_type: str, *, category=None, importance=None) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type,
                         category=category, importance=importance))
        session.commit()
    finally:
        session.close()
    return nid


def _link(a: str, b: str, rel: str = "involves") -> None:
    session = get_session()
    try:
        session.add(Edge(id=str(uuid.uuid4()), source_id=a, target_id=b,
                         relationship_type=rel))
        session.commit()
    finally:
        session.close()


def _findings():
    session = get_session()
    try:
        rows = session.query(KGMaintenanceFinding).filter_by(
            finding_type="state_missing_dates").all()
        session.expunge_all()
        return rows
    finally:
        session.close()


def test_worth_math():
    assert _worth(5.0, [2.0, 8.0]) == 8.0
    assert _worth(5.0, []) == 5.0          # primary-only state → own importance
    assert _worth(0.0, []) == 0.0
    assert date_gap_priority(8.0) == "high"
    assert date_gap_priority(6.0) == "medium"
    assert date_gap_priority(4.5) == "low"


def test_floor_skips_trivia_and_keeps_substance(monkeypatch):
    from app.assistant.utils import identity_names
    monkeypatch.setattr(
        "app.assistant.pipelines.kg_maintenance_pipeline.step_missing_dates_scan."
        "get_required_primary_user_name", lambda: "PrimaryUser",
    )
    user = _mk_node("PrimaryUser", "Entity", importance=10.0)

    chair = _mk_node("Swivel Chair", "Entity", importance=2.0)
    chair_state = _mk_node("Ownership", "State", category="ownership")
    _link(user, chair_state)
    _link(chair_state, chair)

    school = _mk_node("UC Berkeley", "Entity", importance=8.0)
    edu_state = _mk_node("Education", "State", category="education")
    _link(user, edu_state)
    _link(edu_state, school)

    summary = run(_Ctx())
    assert summary["new_findings"] == 1
    assert summary["below_floor"] == 1

    rows = _findings()
    assert len(rows) == 1
    f = rows[0]
    assert f.primary_node_id == edu_state
    assert f.priority == "high"
    assert f.evidence_json["worth"] == 8.0


def test_sweep_rejects_existing_below_floor_findings(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.pipelines.kg_maintenance_pipeline.step_missing_dates_scan."
        "get_required_primary_user_name", lambda: "PrimaryUser",
    )
    user = _mk_node("PrimaryUser", "Entity", importance=10.0)
    gadget = _mk_node("Old Gadget", "Entity", importance=1.5)
    gadget_state = _mk_node("Ownership", "State", category="ownership")
    _link(user, gadget_state)
    _link(gadget_state, gadget)

    # Pre-curation backlog: a pending finding for the trivia state.
    session = get_session()
    try:
        session.add(KGMaintenanceFinding(
            id=str(uuid.uuid4()),
            finding_type="state_missing_dates",
            status="pending",
            priority="high",
            primary_node_id=gadget_state,
            suggested_action="ask_user",
            reason="legacy",
        ))
        session.commit()
    finally:
        session.close()

    summary = run(_Ctx())
    assert summary["swept"] == 1
    rows = _findings()
    assert len(rows) == 1
    assert rows[0].status == "rejected"
    assert "curation floor" in rows[0].execution_notes
