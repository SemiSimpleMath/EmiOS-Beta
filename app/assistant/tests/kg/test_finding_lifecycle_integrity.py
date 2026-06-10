"""P0.4 + P3.1 acceptance tests (audit 2026-06-09).

P0.4 — executor integrity:
- atomic investigated→executing claim: concurrent second call skips,
  terminal status is never overwritten
- outcome decided from kg_revision_log ground truth, not the planner's
  self-report (uncorroborated self-report → escalated, never executed)
- manager errors release the claim for a later retry
- drift guard: cited-node substance changed since investigation →
  escalated as stale_recommendation without invoking the manager
- set_status refuses terminal overwrites without allow_terminal_transition
- legacy execute_finding path refuses terminal / in-flight rows

P3.1 — poison-pill quarantine + fair scheduling:
- failed investigations count attempts in evidence_json; third failure
  escalates as investigation_failed_repeatedly
- the pending-claim reserves one slot for the oldest candidate
- order_by_importance is batched and honors stored + edge-derived scores

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_maintenance.store import TERMINAL_STATUSES, execute_finding, set_status
from app.assistant.ServiceLocator.service_locator import DI
from app.models.base import get_session


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_tables():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_node(label: str, *, importance=None) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Entity", importance=importance))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id: str, target_id: str, *, importance=None) -> str:
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(id=eid, source_id=source_id, target_id=target_id,
                         relationship_type="related_to", importance=importance))
        session.commit()
    finally:
        session.close()
    return eid


def _mk_finding(
    *,
    status: str = "investigated",
    primary_node_id: str,
    finding_type: str = "wiki_contradiction",
    report: dict | None = None,
    created_at: datetime | None = None,
    evidence: dict | None = None,
) -> str:
    fid = str(uuid.uuid4())
    session = get_session()
    try:
        f = KGMaintenanceFinding(
            id=fid,
            finding_type=finding_type,
            status=status,
            primary_node_id=primary_node_id,
            suggested_action="review",
            investigation_report_json=report,
            investigated_at=datetime.now(timezone.utc) - timedelta(hours=30),
            evidence_json=evidence,
        )
        if created_at is not None:
            f.created_at = created_at
        session.add(f)
        session.commit()
    finally:
        session.close()
    return fid


def _status_of(fid: str) -> str:
    session = get_session()
    try:
        return session.query(KGMaintenanceFinding).filter_by(id=fid).one().status
    finally:
        session.close()


def _notes_of(fid: str) -> str:
    session = get_session()
    try:
        return session.query(KGMaintenanceFinding).filter_by(id=fid).one().execution_notes or ""
    finally:
        session.close()


def _evidence_of(fid: str) -> dict:
    session = get_session()
    try:
        return dict(session.query(KGMaintenanceFinding).filter_by(id=fid).one().evidence_json or {})
    finally:
        session.close()


def _write_revision_row(finding_id: str | None, *, succeeded: int = 1) -> None:
    session = get_session()
    try:
        session.add(KGRevisionLog(
            id=str(uuid.uuid4()),
            op="update_node_field",
            reason="test mutation",
            finding_id=finding_id,
            succeeded=succeeded,
        ))
        session.commit()
    finally:
        session.close()


REPORT = {
    "recommendation": "Update node X per evidence.",
    "disposition": "auto_apply",
    "take_action": True,
}


class _FakeBlackboard:
    def __init__(self, final_answer_data: dict):
        self._data = final_answer_data

    def get_messages(self):
        return [SimpleNamespace(sender="kg_resolution::final_answer", data=self._data)]


@pytest.fixture
def executor_harness(monkeypatch):
    """Patch manager creation + invocation. Configure per-test via
    harness.on_invoke (callable) and harness.final_answer (dict)."""
    harness = SimpleNamespace(
        invocations=0,
        final_answer={},
        on_invoke=lambda: None,
    )

    def fake_create(name):
        return SimpleNamespace(blackboard=_FakeBlackboard(harness.final_answer))

    def fake_invoke(mgr, msg):
        harness.invocations += 1
        harness.on_invoke()

    monkeypatch.setattr(DI.multi_agent_manager_factory, "create_manager", fake_create)
    monkeypatch.setattr(DI.manager_invoker, "invoke", fake_invoke)
    return harness


# ── P0.4: atomic claim + ground-truth outcome ────────────────────────────


def test_double_call_executes_once_and_keeps_terminal_status(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.final_answer = {"mutations": ["update_node_field on X"]}
    executor_harness.on_invoke = lambda: _write_revision_row(fid)

    first = execute_one(fid)
    assert first["status"] == "executed", first
    assert _status_of(fid) == "executed"
    assert executor_harness.invocations == 1

    second = execute_one(fid)
    assert second["status"] == "skipped", second
    assert _status_of(fid) == "executed"          # never overwritten
    assert executor_harness.invocations == 1      # manager not re-invoked


def test_uncorroborated_self_report_escalates(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.final_answer = {"mutations": ["I totally mutated things"]}
    # on_invoke writes NO revision rows — the self-report is a fabrication.

    result = execute_one(fid)
    assert result["status"] == "escalated", result
    assert result["reason"] == "self_report_not_corroborated"
    assert _status_of(fid) == "escalated"
    assert "not corroborated" in _notes_of(fid)


def test_revision_rows_mark_executed_regardless_of_self_report(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.final_answer = {}  # planner under-reported
    executor_harness.on_invoke = lambda: _write_revision_row(fid)

    result = execute_one(fid)
    assert result["status"] == "executed", result
    assert result["revision_rows"]["tagged"] == 1


def test_true_noop_escalates(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.final_answer = {"result_summary": "could not act"}

    result = execute_one(fid)
    assert result["status"] == "escalated", result
    assert _status_of(fid) == "escalated"


def test_invoke_error_releases_claim_for_retry(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)

    def boom():
        raise RuntimeError("LLM fell over")
    executor_harness.on_invoke = boom

    result = execute_one(fid)
    assert result["status"] == "error"
    assert _status_of(fid) == "investigated"      # claim released, retryable


def test_drift_guard_escalates_without_invoking(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Current Label")
    report = dict(REPORT)
    report["node_snapshot_at_investigation"] = {
        node: {"label": "Old Label", "start_date": None, "end_date": None},
    }
    fid = _mk_finding(primary_node_id=node, report=report)

    result = execute_one(fid)
    assert result["status"] == "escalated", result
    assert result["reason"] == "stale_recommendation"
    assert "label changed" in result["drift"]
    assert "stale_recommendation" in _notes_of(fid)
    assert executor_harness.invocations == 0      # never reached the manager


def test_matching_snapshot_does_not_trip_drift(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Stable Label")
    report = dict(REPORT)
    report["node_snapshot_at_investigation"] = {
        node: {"label": "Stable Label", "start_date": None, "end_date": None,
               "updated_at": "2026-01-01 00:00:00"},  # telemetry-only field ignored
    }
    fid = _mk_finding(primary_node_id=node, report=report)
    executor_harness.on_invoke = lambda: _write_revision_row(fid)

    result = execute_one(fid)
    assert result["status"] == "executed", result


# ── P0.4: terminal-status guard ──────────────────────────────────────────


def test_set_status_refuses_terminal_overwrite():
    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, status="executed")

    with pytest.raises(ValueError, match="terminal"):
        set_status(fid, "escalated", executed_by="test")
    assert _status_of(fid) == "executed"

    # Explicit re-queue is allowed.
    set_status(fid, "pending", executed_by="test", allow_terminal_transition=True)
    assert _status_of(fid) == "pending"


def test_execute_finding_refuses_terminal_and_in_flight():
    node = _mk_node("Subject")
    for status in sorted(TERMINAL_STATUSES) + ["executing"]:
        fid = _mk_finding(primary_node_id=node, status=status)
        result = execute_finding(fid)
        assert result["executed"] is False
        assert "Refused" in result["detail"]
        assert _status_of(fid) == status


# ── P3.1: poison-pill quarantine ─────────────────────────────────────────


def test_three_failed_investigations_quarantine(monkeypatch):
    from app.assistant.kg_investigator import finding_processor

    node = _mk_node("Orphan")
    fid = _mk_finding(
        primary_node_id=node, status="pending",
        finding_type="orphan_node", report=None,
    )

    monkeypatch.setattr(
        DI.multi_agent_manager_factory, "create_manager",
        lambda name: SimpleNamespace(blackboard=_FakeBlackboard({})),
    )

    def fake_invoke(mgr, msg):
        raise RuntimeError("provider quota exhausted")
    monkeypatch.setattr(DI.manager_invoker, "invoke", fake_invoke)

    r1 = finding_processor.investigate_one(fid)
    assert r1["status"] == "error" and r1["attempts"] == 1 and not r1["quarantined"]
    assert _status_of(fid) == "pending"

    r2 = finding_processor.investigate_one(fid)
    assert r2["attempts"] == 2 and not r2["quarantined"]
    assert _status_of(fid) == "pending"

    r3 = finding_processor.investigate_one(fid)
    assert r3["attempts"] == 3 and r3["quarantined"]
    assert _status_of(fid) == "escalated"
    assert "investigation_failed_repeatedly" in _notes_of(fid)
    assert _evidence_of(fid)["investigation_attempts"] == 3


# ── P3.1: fair scheduling + batched importance ───────────────────────────


def test_oldest_pending_gets_reserved_slot():
    from app.assistant.kg_investigator.finding_processor import _claim_pending_finding_ids

    now = datetime.now(timezone.utc)
    # Four young, high-importance findings…
    for i in range(4):
        node = _mk_node(f"Hot{i}", importance=0.9)
        _mk_finding(primary_node_id=node, status="pending",
                    finding_type="orphan_node", created_at=now)
    # …and one old, low-importance one that pure importance-DESC would starve.
    cold = _mk_node("Cold", importance=0.01)
    old_fid = _mk_finding(primary_node_id=cold, status="pending",
                          finding_type="orphan_node",
                          created_at=now - timedelta(days=30))

    claimed = _claim_pending_finding_ids(limit=3)
    assert len(claimed) == 3
    assert old_fid in claimed


def test_order_by_importance_batched_scores():
    from app.assistant.kg_investigator.finding_priority import order_by_importance

    stored = _mk_node("Stored", importance=0.9)
    derived = _mk_node("Derived")              # importance NULL → edge max / 10
    other = _mk_node("Other")
    _mk_edge(derived, other, importance=8.0)   # → 0.8
    bare = _mk_node("Bare")                    # no importance, no edges → 0.0

    now = datetime.now(timezone.utc)
    ordered = order_by_importance([
        ("f_bare", bare, now),
        ("f_stored", stored, now),
        ("f_derived", derived, now),
    ])
    assert ordered == ["f_stored", "f_derived", "f_bare"]
