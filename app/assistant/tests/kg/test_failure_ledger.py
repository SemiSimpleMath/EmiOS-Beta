"""The KG failure ledger (2026-10-05): a failure is counted per item; after SET_ASIDE_AFTER failed runs the item is set
aside (its queue skips it) and a processing_failure finding, born escalated, puts it in front of the owner; a success
clears the count; release returns a set-aside item to its queue.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg import failure_ledger as L
from app.models.base import get_session


def _clean():
    s = get_session()
    try:
        L._ensure(s)
        s.query(L.KGItemFailure).delete()
        s.query(KGMaintenanceFinding).filter(KGMaintenanceFinding.finding_type == "processing_failure").delete()
        s.commit()
    finally:
        s.close()


def test_an_item_failing_in_three_runs_is_set_aside_with_a_finding_and_can_be_released():
    _clean()
    err = RuntimeError("Connection error.")
    assert [L.record_failure("kg_window", "w1", err, label="a window") for _ in range(3)] == [False, False, True]
    assert L.set_aside_ids("kg_window") == {"w1"} and L.set_aside_ids("kg_proposal") == set()
    s = get_session()
    try:
        [f] = s.query(KGMaintenanceFinding).filter(KGMaintenanceFinding.finding_type == "processing_failure").all()
        assert f.status == "escalated" and f.primary_node_id == "w1" and "Connection error" in f.reason
    finally:
        s.close()
    assert L.record_failure("kg_window", "w1", err) is False, "the finding is raised once"
    L.release("kg_window", "w1")
    assert L.set_aside_ids("kg_window") == set()


def test_a_success_clears_the_count():
    _clean()
    L.record_failure("kg_proposal", "p1", RuntimeError("x"))
    L.record_failure("kg_proposal", "p1", RuntimeError("x"))
    L.clear("kg_proposal", "p1")
    assert [L.record_failure("kg_proposal", "p1", RuntimeError("x")) for _ in range(2)] == [False, False]
