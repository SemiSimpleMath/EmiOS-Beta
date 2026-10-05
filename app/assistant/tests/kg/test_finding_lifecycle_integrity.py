"""P0.4 + P3.1 acceptance tests (audit 2026-06-09).

P0.4 — executor integrity:
- atomic investigated→executing claim: concurrent second call skips,
  terminal status is never overwritten
- outcome decided from kg_revision_log ground truth, not the planner's
  self-report (uncorroborated self-report → escalated, never executed)
- drift guard: cited-node substance changed since investigation →
  escalated as stale_recommendation without invoking the manager
- set_status refuses terminal overwrites without allow_terminal_transition
- legacy execute_finding path refuses terminal / in-flight rows

A failed call stops processing (owner, 2026-10-05):
- a manager error, abort (ManagerInvoker's ``manager_aborted`` result) or
  missing manager raises and releases the claim — even after partial
  mutations — so an outage is never recorded as executed / escalated
- an investigation that fails or aborts raises and leaves the finding
  pending, uncounted toward escalation
- both drains stop at the first failure, count it in the KG failure
  ledger, clear it on success, and skip findings the ledger set aside
- a failed importance read raises instead of scoring every finding 0

P3.1 — fair scheduling:
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
from app.assistant.kg import failure_ledger
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_maintenance.store import TERMINAL_STATUSES, execute_finding, set_status
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from app.models.base import get_session


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_tables():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        failure_ledger._ensure(session)
        session.query(failure_ledger.KGItemFailure).delete()
        session.commit()
    finally:
        session.close()
    yield


def _ledger_failures(kind: str, item_id: str) -> int:
    session = get_session()
    try:
        failure_ledger._ensure(session)
        row = session.get(failure_ledger.KGItemFailure, (kind, item_id))
        return row.failures if row else 0
    finally:
        session.close()


def _completed(content: str = "done") -> ToolResult:
    return ToolResult(result_type="final_answer", content=content)


def _aborted(content: str = "max cycles reached") -> ToolResult:
    return ToolResult(result_type="manager_aborted", content=content, data={"aborted": True})


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
    harness.on_invoke (callable returning the ToolResult, or None for a
    completed run) and harness.final_answer (dict)."""
    harness = SimpleNamespace(
        invocations=0,
        final_answer={},
        on_invoke=lambda: None,
    )

    def fake_create(name):
        return SimpleNamespace(blackboard=_FakeBlackboard(harness.final_answer))

    def fake_invoke(mgr, msg):
        harness.invocations += 1
        return harness.on_invoke() or _completed()

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


def test_invoke_error_raises_and_releases_claim_for_retry(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)

    def boom():
        raise RuntimeError("LLM fell over")
    executor_harness.on_invoke = boom

    with pytest.raises(RuntimeError, match="LLM fell over"):
        execute_one(fid)
    assert _status_of(fid) == "investigated"      # claim released, retryable


def test_aborted_run_raises_and_releases_claim_even_after_partial_mutations(executor_harness):
    """An aborted run (max cycles, error exit) returns a manager_aborted
    result whose graceful-exit final answer looks like any other. It must
    not become 'escalated' (no mutations) or 'executed' (partial ones)."""
    from app.assistant.kg_investigator.finding_executor import execute_one
    from app.assistant.kg_investigator.finding_processor import ManagerAborted

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.final_answer = {"mutations": ["update_node_field on X"],
                                     "result_summary": "aborted midway"}

    def partial_then_abort():
        _write_revision_row(fid)
        return _aborted()
    executor_harness.on_invoke = partial_then_abort

    with pytest.raises(ManagerAborted, match="max cycles reached"):
        execute_one(fid)
    assert _status_of(fid) == "investigated"


def test_aborted_run_without_mutations_is_not_escalated(executor_harness):
    from app.assistant.kg_investigator.finding_executor import execute_one
    from app.assistant.kg_investigator.finding_processor import ManagerAborted

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    executor_harness.on_invoke = _aborted

    with pytest.raises(ManagerAborted):
        execute_one(fid)
    assert _status_of(fid) == "investigated"


def test_missing_manager_raises_and_releases_claim(monkeypatch):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    monkeypatch.setattr(DI.multi_agent_manager_factory, "create_manager", lambda name: None)

    with pytest.raises(RuntimeError, match="not registered"):
        execute_one(fid)
    assert _status_of(fid) == "investigated"


def test_run_without_final_answer_raises(executor_harness, monkeypatch):
    from app.assistant.kg_investigator.finding_executor import execute_one

    node = _mk_node("Subject")
    fid = _mk_finding(primary_node_id=node, report=REPORT)
    monkeypatch.setattr(
        DI.multi_agent_manager_factory, "create_manager",
        lambda name: SimpleNamespace(blackboard=SimpleNamespace(get_messages=lambda: [])),
    )

    with pytest.raises(RuntimeError, match="without a final answer"):
        execute_one(fid)
    assert _status_of(fid) == "investigated"


def test_executor_sweep_stops_at_first_failure_and_counts_it(executor_harness):
    from app.assistant.kg_investigator.finding_executor import LEDGER_KIND, run_executable_findings

    first = _mk_finding(primary_node_id=_mk_node("A", importance=0.9), report=REPORT)
    second = _mk_finding(primary_node_id=_mk_node("B", importance=0.1), report=REPORT)

    def boom():
        raise RuntimeError("provider down")
    executor_harness.on_invoke = boom

    with pytest.raises(RuntimeError, match="provider down"):
        run_executable_findings(limit=5)
    assert executor_harness.invocations == 1      # the sweep stopped
    assert _status_of(first) == "investigated"
    assert _status_of(second) == "investigated"
    assert _ledger_failures(LEDGER_KIND, first) == 1
    assert _ledger_failures(LEDGER_KIND, second) == 0

    # The next sweep succeeds: the count is cleared.
    executor_harness.on_invoke = lambda: None
    executor_harness.final_answer = {"result_summary": "declined"}
    result = run_executable_findings(limit=5)
    assert result["processed"] == 2
    assert _ledger_failures(LEDGER_KIND, first) == 0


def test_executor_sweep_skips_set_aside_findings(executor_harness):
    from app.assistant.kg_investigator.finding_executor import LEDGER_KIND, run_executable_findings

    fid = _mk_finding(primary_node_id=_mk_node("A"), report=REPORT)
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(LEDGER_KIND, fid, RuntimeError("refused"))

    result = run_executable_findings(limit=5)
    assert result["status"] == "no_executable_findings"
    assert executor_harness.invocations == 0
    assert _status_of(fid) == "investigated"


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


# ── A failed investigation stops processing ──────────────────────────────


INVESTIGATION_REPORT = {
    "take_action": True,
    "recommendation": "Link the orphan to its parent.",
    "disposition": "auto_apply",
}


@pytest.fixture
def investigator(monkeypatch):
    """Patch the investigation manager. harness.results is consumed one per
    invocation: an Exception raises, a ToolResult is returned."""
    harness = SimpleNamespace(invocations=[], results=[])

    monkeypatch.setattr(
        DI.multi_agent_manager_factory, "create_manager",
        lambda name: SimpleNamespace(blackboard=SimpleNamespace(get_messages=lambda: [
            SimpleNamespace(sender="kg_investigation::final_answer", data=INVESTIGATION_REPORT),
        ])),
    )

    def fake_invoke(mgr, msg):
        harness.invocations.append(msg)
        outcome = harness.results.pop(0) if harness.results else _completed()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(DI.manager_invoker, "invoke", fake_invoke)
    return harness


def _pending_orphan(label: str, *, importance=None) -> str:
    return _mk_finding(primary_node_id=_mk_node(label, importance=importance),
                       status="pending", finding_type="orphan_node", report=None)


@pytest.mark.parametrize("outcome", [RuntimeError("provider quota exhausted"), _aborted()])
def test_failed_investigation_raises_and_leaves_finding_pending(investigator, outcome):
    """An outage is not a verdict: no escalation, no attempts counter, the
    finding stays pending for the next run."""
    from app.assistant.kg_investigator import finding_processor

    fid = _pending_orphan("Orphan")
    for _ in range(4):
        investigator.results = [outcome]
        with pytest.raises(Exception):
            finding_processor.investigate_one(fid)
    assert _status_of(fid) == "pending"
    assert "investigation_attempts" not in _evidence_of(fid)


def test_run_without_structured_report_raises(investigator, monkeypatch):
    from app.assistant.kg_investigator import finding_processor

    fid = _pending_orphan("Orphan")
    monkeypatch.setattr(finding_processor, "_extract_report_from_audit", lambda bb: None)

    with pytest.raises(RuntimeError, match="without a structured report"):
        finding_processor.investigate_one(fid)
    assert _status_of(fid) == "pending"


def test_investigation_drain_stops_at_first_failure_and_counts_it(investigator):
    from app.assistant.kg_investigator.finding_processor import LEDGER_KIND, investigate_findings

    first = _pending_orphan("A")
    second = _pending_orphan("B")
    investigator.results = [_aborted()]

    with pytest.raises(Exception, match="aborted"):
        investigate_findings([first, second])
    assert len(investigator.invocations) == 1     # the run stopped
    assert _status_of(first) == "pending"
    assert _status_of(second) == "pending"
    assert _ledger_failures(LEDGER_KIND, first) == 1
    assert _ledger_failures(LEDGER_KIND, second) == 0

    # The next run succeeds: both investigated, the count cleared.
    result = investigate_findings([first, second])
    assert result["investigated"] == 2
    assert _status_of(first) == "investigated"
    assert _ledger_failures(LEDGER_KIND, first) == 0


def test_run_pending_findings_propagates_the_failure(investigator):
    from app.assistant.kg_investigator.finding_processor import LEDGER_KIND, run_pending_findings

    fid = _pending_orphan("A")
    investigator.results = [RuntimeError("Connection error.")]

    with pytest.raises(RuntimeError, match="Connection error"):
        run_pending_findings(limit=5, finding_types=["orphan_node"])
    assert _ledger_failures(LEDGER_KIND, fid) == 1


def test_set_aside_findings_are_skipped_by_the_investigation_queue(investigator):
    from app.assistant.kg_investigator.finding_processor import (
        LEDGER_KIND, _claim_pending_finding_ids, investigate_findings,
    )

    stuck = _pending_orphan("Stuck", importance=0.9)
    other = _pending_orphan("Other", importance=0.1)
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(LEDGER_KIND, stuck, RuntimeError("refused"))

    assert _claim_pending_finding_ids(limit=5, finding_types=["orphan_node"]) == [other]
    result = investigate_findings([stuck, other])
    assert result["processed"] == 1
    assert _status_of(stuck) == "pending"


def test_apply_one_raises_on_an_aborted_run(investigator):
    from app.assistant.kg_investigator.finding_processor import ManagerAborted, apply_one

    fid = _mk_finding(primary_node_id=_mk_node("Subject"), report=REPORT)
    investigator.results = [_aborted()]

    with pytest.raises(ManagerAborted):
        apply_one(fid)
    assert _status_of(fid) == "investigated"


def test_failed_importance_read_raises(monkeypatch):
    from app.assistant.kg_investigator import finding_priority

    class _Broken:
        def read_session(self):
            raise RuntimeError("database is locked")
    monkeypatch.setattr(finding_priority, "get_db_manager", lambda: _Broken())

    with pytest.raises(RuntimeError, match="database is locked"):
        finding_priority.order_by_importance([("f1", "n1", None)])


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
