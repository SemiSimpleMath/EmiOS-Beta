"""Tests for the state_missing_dates investigator brief.

Doesn't run the LLM — just verifies that build_finding_brief returns the
right specialized brief for state_missing_dates, with the dated-neighbor
anchors and evidence-window context wired in. The investigator's prompt
quality is a separate concern handled by manual review of the agent's
system prompt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_investigator.finding_brief import build_finding_brief
from app.assistant.kg_maintenance.store import upsert_finding
from app.models.base import get_session


@pytest.fixture(autouse=True)
def _seed(kg_clean_db):
    """Recreate maintenance table (kg_clean_db only truncates) and seed
    a focused mini-graph for date-investigator tests."""
    session = get_session()
    engine = session.bind
    session.close()
    KGMaintenanceFinding.__table__.drop(engine, checkfirst=True)
    KGMaintenanceFinding.__table__.create(engine, checkfirst=True)

    session = get_session()
    try:
        # Subject: a Residence state on Annika with no dates.
        session.add(Node(
            id="11111111-1111-1111-1111-111111111111",
            label="Residence at Cabin",
            node_type="State",
            category="residence",
            description="Annika living at the cabin",
            start_date=None,
            end_date=None,
        ))
        # Annika herself
        session.add(Node(
            id="22222222-2222-2222-2222-222222222222",
            label="Annika",
            node_type="Person",
        ))
        # A dated neighbor — gives the investigator a temporal anchor.
        session.add(Node(
            id="33333333-3333-3333-3333-333333333333",
            label="Cabin trip",
            node_type="Event",
            start_date=datetime(2010, 6, 1),
            end_date=datetime(2010, 6, 8),
        ))
        # Edges
        session.add(Edge(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            source_id="22222222-2222-2222-2222-222222222222",
            target_id="11111111-1111-1111-1111-111111111111",
            relationship_type="lives_in",
            sentence="Annika is residing at the cabin.",
        ))
        session.add(Edge(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            source_id="11111111-1111-1111-1111-111111111111",
            target_id="33333333-3333-3333-3333-333333333333",
            relationship_type="overlaps_with",
            sentence="Residence overlapped with cabin trip.",
        ))
        session.commit()
    finally:
        session.close()


def test_state_missing_dates_brief_uses_specialized_task():
    fid, _ = upsert_finding(
        finding_type="state_missing_dates",
        primary_node_id="11111111-1111-1111-1111-111111111111",
        suggested_action="fill_dates",
        reason="State 'Residence at Cabin' has no start_date — connects 2 entities.",
        confidence=None,
        priority="medium",
        agent_name="step_missing_dates_scan",
        evidence={"category": "residence", "top_pageranks": [0.8, 0.5]},
    )

    result = build_finding_brief(fid)
    assert result is not None
    task, information = result

    # Specialized task wording — not the generic fallback.
    assert "fill_dates" in task
    assert "dated neighbors" in task.lower() or "dated" in task.lower()
    assert "ISO YYYY-MM-DD" in task or "YYYY-MM-DD" in task
    assert "auto-applied" in task.lower() or "executor" in task.lower()


def test_state_missing_dates_brief_includes_dated_neighbor():
    fid, _ = upsert_finding(
        finding_type="state_missing_dates",
        primary_node_id="11111111-1111-1111-1111-111111111111",
        suggested_action="fill_dates",
        reason="Residence has no start_date.",
        priority="medium",
        agent_name="step_missing_dates_scan",
    )

    _, information = build_finding_brief(fid)
    # The Cabin trip event (2010-06-01 → 2010-06-08) must surface as a
    # temporal anchor — this is the WHOLE POINT of the specialized brief.
    assert "Dated neighbors" in information
    assert "Cabin trip" in information
    assert "2010-06-01" in information


def test_state_missing_dates_brief_falls_back_when_no_dated_neighbors():
    """Subject with zero dated neighbors should still get the specialized
    brief, just with a "(none)" line rather than crashing."""
    # Strip the dated-neighbor edge + node
    session = get_session()
    try:
        session.query(Edge).filter(Edge.id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb").delete()
        session.query(Node).filter(Node.id == "33333333-3333-3333-3333-333333333333").delete()
        session.commit()
    finally:
        session.close()

    fid, _ = upsert_finding(
        finding_type="state_missing_dates",
        primary_node_id="11111111-1111-1111-1111-111111111111",
        suggested_action="fill_dates",
        reason="Residence has no start_date.",
        priority="medium",
        agent_name="step_missing_dates_scan",
    )

    _, information = build_finding_brief(fid)
    assert "Dated neighbors" in information
    assert "(none" in information.lower() or "no connected node has dates" in information.lower()


def test_unknown_finding_type_still_falls_back_to_generic_brief():
    """The new state_missing_dates branch must not break the generic fallback
    for other types."""
    fid, _ = upsert_finding(
        finding_type="some_future_type_we_havent_added_yet",
        primary_node_id="22222222-2222-2222-2222-222222222222",
        suggested_action="review",
        reason="hypothetical",
        priority="low",
        agent_name="test",
    )

    result = build_finding_brief(fid)
    assert result is not None
    task, information = result
    # Falls through to _brief_single_node generic prompt.
    assert "Investigate this finding" in task
