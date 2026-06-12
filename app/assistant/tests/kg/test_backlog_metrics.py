"""Maintenance backlog metrics (fragility review #2, move 3)."""
from __future__ import annotations

import uuid
from datetime import timedelta

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg_maintenance.backlog_metrics import compute_backlog_metrics
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session


def _mk_finding(ftype, status, created_at=None, updated_at=None):
    fid = str(uuid.uuid4())
    session = get_session()
    try:
        f = KGMaintenanceFinding(
            id=fid, finding_type=ftype, status=status,
            primary_node_id=str(uuid.uuid4()), suggested_action="review",
        )
        if created_at is not None:
            f.created_at = created_at
        if updated_at is not None:
            f.updated_at = updated_at
        session.add(f)
        session.commit()
    finally:
        session.close()
    return fid


def test_backlog_metrics_counts_ages_and_trend():
    session = get_session()
    try:
        session.query(KGMaintenanceFinding).delete()
        session.commit()
    finally:
        session.close()

    now = utc_now()
    old = now - timedelta(days=30)

    _mk_finding("duplicate_node", "pending", created_at=old)          # old open
    _mk_finding("duplicate_node", "pending", created_at=now)          # raised in window
    _mk_finding("date_lint", "investigated", created_at=now)          # open, raised
    _mk_finding("date_lint", "rejected", created_at=old, updated_at=now)  # drained
    _mk_finding("orphan_node", "executed", created_at=old, updated_at=old)  # old close

    m = compute_backlog_metrics(window_days=7)

    assert m["total_open"] == 3
    assert m["open_by_type"] == {"duplicate_node": 2, "date_lint": 1}
    assert m["oldest_open_age_days"] >= 29
    assert m["raised_7d"] == 2
    assert m["drained_7d"] == 1
    assert m["drain_deficit"] == 1
