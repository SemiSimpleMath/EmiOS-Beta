"""Date compare module + date lint scan (fragility review #5, 2026-06-12).

Dates route facts now (temporal drain, era splits, UPCOMING markers,
identity-sentence eras) — comparison logic lives in ONE module and a lint
scan finds doctrine violations before they mislead agents.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_core.kg_utils.date_compare import (
    as_aware_utc,
    end_before_start,
    in_window,
    is_future,
    is_past,
    windows_overlap,
)
from app.assistant.pipelines.kg_maintenance_pipeline.step_date_lint_scan import (
    _node_issues,
    run as run_lint,
)
from app.models.base import get_session

D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731
NAIVE = lambda y, m=1, d=1: datetime(y, m, d)  # noqa: E731


# ── compare module ───────────────────────────────────────────────────────


def test_aware_normalization_and_relatives():
    assert as_aware_utc(None) is None
    assert as_aware_utc(NAIVE(2020)).tzinfo is not None
    now = D(2026, 6, 12)
    assert is_future(D(2026, 7, 1), now=now)
    assert is_past(NAIVE(2020), now=now)        # naive input normalized
    assert not is_future(None, now=now)


def test_in_window_half_open_and_unbounded():
    assert in_window(D(2023), D(2020), D(2024))
    assert in_window(D(2020), D(2020), D(2024))      # inclusive start
    assert not in_window(D(2024), D(2020), D(2024))  # exclusive end
    assert in_window(D(2030), D(2020), None)          # open end
    assert in_window(D(1999), None, D(2024))          # open start
    assert not in_window(None, D(2020), D(2024))


def test_windows_overlap_and_impossible_era():
    assert windows_overlap(D(2020), D(2024), D(2023), D(2026))
    assert not windows_overlap(D(2020), D(2022), D(2022), D(2024))  # half-open touch
    assert windows_overlap(D(2020), None, D(2030), D(2031))          # open end
    assert end_before_start(D(2024), D(2020))
    assert not end_before_start(D(2020), D(2024))
    assert not end_before_start(D(2020), None)


# ── lint checks (pure) ───────────────────────────────────────────────────


def _node(**kw):
    base = dict(start_date=None, end_date=None, start_date_confidence=None,
                end_date_confidence=None, original_sentence="", description="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_lint_flags_impossible_era():
    issues = _node_issues(_node(start_date=D(2024), end_date=D(2020)))
    assert any("impossible era" in i for i in issues)


def test_lint_flags_ongoing_contradiction_irvine_shape():
    # The live exhibit: "moved to Irvine and are here ever since" + end_date.
    issues = _node_issues(_node(
        start_date=D(2010, 6, 1), end_date=D(2011, 11, 23),
        original_sentence="They move to the city and are here ever since.",
    ))
    assert any("ongoing contradiction" in i for i in issues)


def test_lint_flags_undocumented_floor_and_unknown_confidence():
    issues = _node_issues(_node(start_date=D(2003, 1, 1)))  # Jan-1, no conf
    assert any("undocumented floor" in i for i in issues)

    issues = _node_issues(_node(start_date=D(2003, 5, 2),
                                start_date_confidence="guessed"))
    assert any("unknown confidence" in i for i in issues)


def test_lint_passes_clean_nodes():
    assert _node_issues(_node(
        start_date=D(2003, 1, 1), start_date_confidence="estimated",
        original_sentence="It began around 2003.",
    )) == []
    assert _node_issues(_node(
        start_date=D(2010, 6, 1), end_date=D(2012, 3, 4),
        end_date_confidence="actual",
        original_sentence="They lived there for two years.",
    )) == []


# ── scan end to end ──────────────────────────────────────────────────────


def test_scan_files_review_findings():
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.query(KGMaintenanceFinding).delete()
        session.add(Node(
            id=nid, label="Broken Era", node_type="State",
            start_date=D(2024), end_date=D(2020),
        ))
        session.add(Node(
            id=str(uuid.uuid4()), label="Fine Node", node_type="State",
            start_date=D(2020), start_date_confidence="actual",
        ))
        session.commit()
    finally:
        session.close()

    result = run_lint(SimpleNamespace(run_id="test-run"))
    assert result["new_findings"] == 1

    session = get_session()
    try:
        f = (session.query(KGMaintenanceFinding)
             .filter_by(finding_type="date_lint", primary_node_id=nid).one())
        assert f.status == "investigated"
        report = dict(f.investigation_report_json or {})
        assert report.get("disposition") == "needs_user_review"
        assert "kg_update_node_field" in (report.get("recommendation") or "")
    finally:
        session.close()
