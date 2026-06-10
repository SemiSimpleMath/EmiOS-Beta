"""P3.2 (audit 2026-06-09): the investigator + executor claim queries must
skip findings with ``superseded_by`` set. Superseded siblings are resolved
by their cluster lead's status cascade; claiming them independently
duplicates LLM spend and can double-execute against the same root cause.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg_investigator.finding_executor import _claim_executable_finding_ids
from app.assistant.kg_investigator.finding_processor import _claim_pending_finding_ids
from app.assistant.kg_maintenance.store import upsert_finding
from app.models.base import get_session


def _mk_finding(*, secondary: str, status: str = "pending",
                superseded_by: str | None = None,
                report: dict | None = None,
                investigated_hours_ago: float | None = None) -> str:
    fid, _ = upsert_finding(
        finding_type="wiki_contradiction",
        primary_node_id="primaryaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        secondary_node_id=secondary,
        suggested_action="review",
        reason="test",
        confidence=0.7,
        priority="medium",
        agent_name="test",
    )
    session = get_session()
    try:
        f = session.query(KGMaintenanceFinding).filter_by(id=fid).one()
        f.status = status
        f.superseded_by = superseded_by
        if report is not None:
            f.investigation_report_json = report
        if investigated_hours_ago is not None:
            f.investigated_at = datetime.now(timezone.utc) - timedelta(hours=investigated_hours_ago)
        session.commit()
    finally:
        session.close()
    return fid


def test_pending_claim_skips_superseded():
    lead = _mk_finding(secondary="sec-1")
    sibling = _mk_finding(secondary="sec-2", superseded_by=lead)

    claimed = _claim_pending_finding_ids(limit=10)

    assert lead in claimed
    assert sibling not in claimed


def test_executable_claim_skips_superseded():
    report = {"disposition": "auto_apply", "recommendation": "do the thing"}
    lead = _mk_finding(
        secondary="sec-1", status="investigated",
        report=report, investigated_hours_ago=30,
    )
    sibling = _mk_finding(
        secondary="sec-2", status="investigated",
        superseded_by=lead, report=report, investigated_hours_ago=30,
    )

    claimed = _claim_executable_finding_ids(limit=10)

    assert lead in claimed
    assert sibling not in claimed
