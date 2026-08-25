"""A 'regression' means a fix did not hold — not merely a repeat subsystem.

2026-08-25: 16 of 20 inbox cases carried [REGRESSION]. The flag fired whenever ANY
previously-resolved case shared the implicated subsystem, and subsystems are coarse
('scheduler', 'dayflow'), so one resolved case marked every later case in that area
as a regression forever. The tag stopped carrying information.

A fix that did not hold fails soon after it ships; the same broad subsystem
surfacing months later is ordinary new work. The match is now bounded by
_REGRESSION_WINDOW_DAYS.
"""
from __future__ import annotations

from datetime import timedelta

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.database.system_audit_case import SystemAuditCase
from app.assistant.system_audit import case_store
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session


def _open_case(summary: str) -> str:
    """Open a case and walk it to `assembled` — the state mark_investigated expects
    (the real chain is open -> assembled -> investigated)."""
    cid = case_store.open_case(
        trigger_kind="auditor_finding",
        summary=summary,
        room_id="dayflow_orchestrator",
        bound_ids={"work_ids": []},
    )
    case_store.transition(cid, "assembled")
    return cid


def _resolve_with_age(case_id: str, *, subsystem: str, days_ago: float) -> None:
    """Drive a case to resolved, then age its updated_at to simulate an older fix."""
    case_store.mark_investigated(case_id, preliminary_read="read", implicated_subsystem=subsystem,
                                 repair_suggestions=[], confidence=0.9)
    case_store.transition(case_id, "resolved")
    session = get_session()
    try:
        row = session.get(SystemAuditCase, case_id)
        row.updated_at = utc_now() - timedelta(days=days_ago)
        session.commit()
    finally:
        session.close()


def _status_of(case_id: str) -> str:
    session = get_session()
    try:
        return session.get(SystemAuditCase, case_id).status
    finally:
        session.close()


def test_recent_resolved_case_in_same_subsystem_marks_regression():
    old = _open_case("first scheduler finding")
    _resolve_with_age(old, subsystem="scheduler_win", days_ago=1)

    new = _open_case("second scheduler finding")
    status = case_store.mark_investigated(new, preliminary_read="read",
                                          implicated_subsystem="scheduler_win",
                                          repair_suggestions=[], confidence=0.9)
    assert status == "regressed"
    assert _status_of(new) == "regressed"


def test_long_resolved_case_does_not_mark_regression():
    old = _open_case("ancient scheduler finding")
    _resolve_with_age(old, subsystem="scheduler_old",
                      days_ago=case_store._REGRESSION_WINDOW_DAYS + 5)

    new = _open_case("new scheduler finding")
    status = case_store.mark_investigated(new, preliminary_read="read",
                                          implicated_subsystem="scheduler_old",
                                          repair_suggestions=[], confidence=0.9)
    assert status == "investigated"
    assert _status_of(new) == "investigated"


def test_unrelated_subsystem_never_marks_regression():
    old = _open_case("recent dayflow finding")
    _resolve_with_age(old, subsystem="dayflow_x", days_ago=1)

    new = _open_case("kg finding")
    status = case_store.mark_investigated(new, preliminary_read="read",
                                          implicated_subsystem="kg_core_x",
                                          repair_suggestions=[], confidence=0.9)
    assert status == "investigated"
