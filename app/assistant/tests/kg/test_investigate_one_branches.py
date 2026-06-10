"""Unit tests for `finding_processor.investigate_one`'s post-LLM branches.

Covers the orchestration logic between the investigator manager and the
finding's terminal state — the path the verdict_store unit tests don't
exercise:

- take_action=False with valid verdict fields → finding closes 'dismissed',
  verdict row written
- take_action=False with missing verdict fields → finding closes 'escalated'
  (contract violation, no silent-dismiss-without-verdict)
- take_action=False with unknown verdict_type → store rejects, finding
  closes 'escalated' (not dismissed)
- take_action=False with verdict_type+memo but blank verdict_node_ids →
  falls back to finding's primary/secondary node ids
- take_action=True → finding stays 'investigated' (no premature close)

LLM invocation is stubbed: `_extract_report_from_audit` is patched to
return a hand-crafted report dict, and the manager invocation is a
no-op. We're testing orchestration, not the LLM.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.kg_maintenance.verdict_store import (
    canonical_pair,
    get_verdicts_for_pair,
    get_verdicts_for_node,
)
from app.models.base import get_session


def _seed_finding(*, primary="X", secondary="Y", finding_type="duplicate_node"):
    """Create a pending finding for the test to investigate.

    Real Node rows back the ids: upsert_finding refuses duplicate_node
    findings whose ids aren't live, and investigate_one refuses verdicts
    naming non-live ids (audit P1.1/P1.4)."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    s = get_session()
    try:
        for nid in (primary, secondary):
            if nid and s.query(Node).filter_by(id=nid).count() == 0:
                s.add(Node(id=nid, label=f"test-{nid}", node_type="Entity"))
        s.commit()
    finally:
        s.close()

    fid, _ = upsert_finding(
        finding_type=finding_type,
        primary_node_id=primary,
        secondary_node_id=secondary,
        suggested_action="merge",
        reason="test seed",
        confidence=0.8,
        priority="medium",
        agent_name="test",
    )
    return fid


def _read_finding(finding_id):
    s = get_session()
    try:
        f = s.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        # Detach so the test can inspect after the session closes.
        if f is not None:
            s.expunge(f)
        return f
    finally:
        s.close()


def _patch_investigation(report_dict):
    """Returns a context manager that replaces the LLM investigation
    machinery with stubs returning *report_dict*. The brief builder
    still runs (real DB read on the seeded finding) — only the manager
    invocation + report extraction are stubbed."""
    fake_mgr = MagicMock()
    fake_mgr.blackboard = MagicMock()

    return patch.multiple(
        "app.assistant.kg_investigator.finding_processor",
        _extract_report_from_audit=MagicMock(return_value=report_dict),
        DI=MagicMock(
            multi_agent_manager_factory=MagicMock(
                create_manager=MagicMock(return_value=fake_mgr),
            ),
            manager_invoker=MagicMock(invoke=MagicMock(return_value=None)),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# take_action=False: the verdict path
# ─────────────────────────────────────────────────────────────────────────────


def test_take_action_false_with_full_verdict_writes_verdict_and_dismisses():
    fid = _seed_finding(primary="X", secondary="Y")
    report = {
        "take_action": False,
        "verdict_type": "distinct",
        "verdict_memo": "do not merge X with Y — different referents",
        "verdict_node_ids": ["X", "Y"],
        "recommendation": "These are distinct things.",
        "confidence": 0.9,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "dismissed"
    assert result["verdict_id"]

    f = _read_finding(fid)
    assert f.status == "dismissed"
    assert "no action needed" in (f.execution_notes or "").lower()

    # Verdict row exists, canonical-ordered.
    hits = get_verdicts_for_pair("X", "Y")
    assert len(hits) == 1
    assert hits[0].verdict_type == "distinct"
    assert hits[0].source_finding_id == fid


def test_take_action_false_missing_verdict_fields_escalates_not_dismisses():
    """The investigator broke the contract — emitted take_action=False
    without a verdict. Silently dismissing would lose the finding
    forever; we escalate so the breakage surfaces."""
    fid = _seed_finding(primary="X", secondary="Y")
    report = {
        "take_action": False,
        # Note: no verdict_type, verdict_memo, or verdict_node_ids.
        "recommendation": "I think these are distinct.",
        "confidence": 0.5,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "escalated"
    assert result["reason"] == "take_action_false_contract_violation"

    f = _read_finding(fid)
    assert f.status == "escalated"

    # No verdict row written.
    assert get_verdicts_for_pair("X", "Y") == []


def test_take_action_false_unknown_verdict_type_escalates():
    """verdict_store rejects unknown vocabulary; orchestration must
    escalate rather than silently dismiss with no verdict on file."""
    fid = _seed_finding(primary="X", secondary="Y")
    report = {
        "take_action": False,
        "verdict_type": "made_up_category",  # not in VALID_VERDICT_TYPES
        "verdict_memo": "do not merge X with Y",
        "verdict_node_ids": ["X", "Y"],
        "recommendation": "test",
        "confidence": 0.9,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "escalated"
    assert result["reason"] == "verdict_write_rejected"

    f = _read_finding(fid)
    assert f.status == "escalated"
    assert get_verdicts_for_pair("X", "Y") == []


def test_take_action_false_falls_back_to_finding_node_ids():
    """When investigator gives verdict_type+memo but skips
    verdict_node_ids, finding_processor should fall back to the
    finding's primary/secondary ids — keeps the verdict discoverable
    even with a partial-contract LLM output."""
    fid = _seed_finding(primary="alpha", secondary="beta")
    report = {
        "take_action": False,
        "verdict_type": "distinct",
        "verdict_memo": "do not merge alpha and beta — distinct",
        "verdict_node_ids": [],  # empty; should fall back to (alpha, beta)
        "recommendation": "test",
        "confidence": 0.9,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "dismissed"
    hits = get_verdicts_for_pair("alpha", "beta")
    assert len(hits) == 1
    a, b = canonical_pair("alpha", "beta")
    assert (hits[0].node_id_a, hits[0].node_id_b) == (a, b)


# ─────────────────────────────────────────────────────────────────────────────
# take_action=True: stays 'investigated'
# ─────────────────────────────────────────────────────────────────────────────


def test_take_action_true_leaves_status_investigated():
    """take_action=True hands off to the executor pipeline (24h grace or
    user review). investigate_one should NOT auto-close anything."""
    fid = _seed_finding(primary="X", secondary="Y")
    report = {
        "take_action": True,
        "recommendation": "Merge X into Y because they're the same.",
        "disposition": "auto_apply",
        "confidence": 0.95,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "investigated"

    f = _read_finding(fid)
    assert f.status == "investigated"
    # No verdict for take_action=True path.
    assert get_verdicts_for_node("X") == []
    assert get_verdicts_for_node("Y") == []


def test_take_action_true_needs_user_review_does_not_close():
    """Same: needs_user_review path waits for the user, doesn't dismiss."""
    fid = _seed_finding(primary="X", secondary="Y")
    report = {
        "take_action": True,
        "recommendation": "Merge X with Y; user should confirm.",
        "disposition": "needs_user_review",
        "user_question": "Merge X into Y?",
        "confidence": 0.6,
        "diagnosis": "test",
    }
    with _patch_investigation(report):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "investigated"

    f = _read_finding(fid)
    assert f.status == "investigated"


# ─────────────────────────────────────────────────────────────────────────────
# Defensive: malformed inputs from earlier in the pipeline
# ─────────────────────────────────────────────────────────────────────────────


def test_no_report_extracted_returns_no_report_status():
    """If the manager runs but produces no structured report, finding
    stays at pending and we return early."""
    fid = _seed_finding()

    # Stub _extract_report_from_audit to return None (no report found).
    fake_mgr = MagicMock()
    fake_mgr.blackboard = MagicMock()
    with patch.multiple(
        "app.assistant.kg_investigator.finding_processor",
        _extract_report_from_audit=MagicMock(return_value=None),
        DI=MagicMock(
            multi_agent_manager_factory=MagicMock(
                create_manager=MagicMock(return_value=fake_mgr),
            ),
            manager_invoker=MagicMock(invoke=MagicMock(return_value=None)),
        ),
    ):
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(fid)

    assert result["status"] == "no_report"
    f = _read_finding(fid)
    assert f.status == "pending"  # untouched
