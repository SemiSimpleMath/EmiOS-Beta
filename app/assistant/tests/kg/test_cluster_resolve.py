"""End-to-end tests for the kg_finding_cluster_resolver pipeline.

The agent itself is mocked — we test the candidate generator + verdict
applier + cascade behavior + read-time enrichment. Keeps tests fast and
deterministic; the agent's prompt is a separate concern.

Scenarios:
  - Single-finding "cluster" is a no-op (no LLM call, no superseded_by).
  - Multi-finding cluster on one primary → agent verdict is_same_root=True
    → siblings get superseded_by=lead, lead gets cluster block, cascade
    on lead's status flips siblings.
  - Agent verdict is_same_root=False → no findings get superseded.
  - Agent excludes some findings from member_finding_ids → excluded ones
    stay independent (not superseded), only included siblings are.
  - get_findings default-hides superseded; get_summary_counts likewise;
    cluster_size is stamped on the lead.
  - state_auto_closed is excluded from candidate clustering (pure log).
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_maintenance.store import (
    get_findings,
    get_summary_counts,
    set_status,
    upsert_finding,
)
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_maintenance_pipeline.step_cluster_resolve import (
    _build_candidate_clusters,
    run as run_cluster_resolve,
)
from app.models.base import get_session


# ── Fixtures / helpers ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _seed_node(kg_clean_db):
    """The shared kg_clean_db autouse already truncates Node/Edge. Add the
    one node our test findings reference so _enrich_node_labels has
    something to find. Also drop+recreate kg_maintenance_finding so the
    test DB picks up the latest schema (the shared fixture doesn't
    Base.metadata.drop_all, so prior-run tables survive with old columns)."""
    from app.models.base import Base

    session = get_session()
    engine = session.bind
    session.close()

    # Force the kg_maintenance_finding table to track the current model.
    KGMaintenanceFinding.__table__.drop(engine, checkfirst=True)
    KGMaintenanceFinding.__table__.create(engine, checkfirst=True)

    session = get_session()
    try:
        session.add(Node(
            id="anniaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            label="Annika",
            node_type="Person",
            description="Test node",
        ))
        session.add(Node(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            label="Other Person",
            node_type="Person",
            description="Test node 2",
        ))
        session.commit()
    finally:
        session.close()


def _wipe_findings():
    session = get_session()
    try:
        session.query(KGMaintenanceFinding).delete()
        session.commit()
    finally:
        session.close()


def _make_finding(
    *,
    primary_node_id: str = "anniaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    secondary_node_id: str | None = None,
    finding_type: str = "wiki_contradiction",
    reason: str = "test",
    confidence: float = 0.7,
    priority: str = "medium",
    suggested_action: str = "review",
) -> str:
    fid, _ = upsert_finding(
        finding_type=finding_type,
        primary_node_id=primary_node_id,
        secondary_node_id=secondary_node_id,
        suggested_action=suggested_action,
        reason=reason,
        confidence=confidence,
        priority=priority,
        agent_name="test",
    )
    return fid


_PRIMARY_NODE_ID = "anniaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SECONDARY_COUNTER = [0]


def _force_unique_finding(*, finding_type: str, reason: str) -> str:
    """upsert_finding dedups by (finding_type, primary, secondary). To get
    multiple findings on the same primary in tests, we use distinct fake
    secondaries.

    Important: upsert_finding normalizes pair ordering by sorting (swaps
    primary/secondary if primary > secondary). The test secondaries must
    therefore be guaranteed alphabetically GREATER than the test primary
    or the swap will rewrite our primary out from under us. Using a
    'zzzz' prefix keeps the fake secondary > any realistic primary."""
    _SECONDARY_COUNTER[0] += 1
    fake_secondary = f"zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzz{_SECONDARY_COUNTER[0]:04d}"
    return _make_finding(
        finding_type=finding_type,
        secondary_node_id=fake_secondary,
        reason=reason,
    )


# ── Stub agent factory ──────────────────────────────────────────────────


class StubAgentResult:
    def __init__(self, data: dict):
        self.data = data


class StubAgent:
    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.calls = []

    def action_handler(self, message):
        self.calls.append(message)
        return StubAgentResult(self.verdict)


def _patch_agent(verdict: dict):
    return patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        return_value=StubAgent(verdict),
    )


# ── Tests ───────────────────────────────────────────────────────────────


def test_single_finding_is_not_a_candidate_cluster():
    _wipe_findings()
    _make_finding(reason="single thing")

    candidates = _build_candidate_clusters()
    # Group of 1 — does not qualify
    assert candidates == {}


def test_state_auto_closed_excluded_from_candidates():
    _wipe_findings()
    # Two state_auto_closed on the same node — must not be candidate
    _force_unique_finding(finding_type="state_auto_closed", reason="closed 1")
    _force_unique_finding(finding_type="state_auto_closed", reason="closed 2")

    candidates = _build_candidate_clusters()
    assert candidates == {}


def test_multi_finding_cluster_with_same_root_sets_superseded_by():
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="page says art lessons stopped")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="page says art ended in March")
    fid_c = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="card says she still does art")

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b, fid_c],
        "root_question": "Did Annika stop taking art lessons? When?",
        "reason": "all three reference the same fact about art lessons",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")

    with _patch_agent(verdict):
        result = run_cluster_resolve(ctx)

    assert result["candidates_examined"] == 1
    assert result["clusters_confirmed"] == 1
    assert result["findings_superseded"] == 2

    # Verify DB state
    session = get_session()
    try:
        rows = {r.id: r for r in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()
    assert rows[fid_a].superseded_by is None  # lead
    assert rows[fid_b].superseded_by == fid_a
    assert rows[fid_c].superseded_by == fid_a
    assert isinstance(rows[fid_a].evidence_json, dict)
    assert "cluster" in rows[fid_a].evidence_json
    cluster = rows[fid_a].evidence_json["cluster"]
    assert cluster["root_question"] == "Did Annika stop taking art lessons? When?"
    assert sorted(cluster["sibling_ids"]) == sorted([fid_b, fid_c])


def test_is_same_root_false_does_not_supersede():
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="art lessons")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="grade level disagreement")

    verdict = {
        "is_same_root": False,
        "lead_finding_id": None,
        "member_finding_ids": [],
        "root_question": None,
        "reason": "different facts",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")

    with _patch_agent(verdict):
        result = run_cluster_resolve(ctx)

    assert result["clusters_confirmed"] == 0
    assert result["findings_superseded"] == 0

    session = get_session()
    try:
        rows = {r.id: r for r in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()
    assert rows[fid_a].superseded_by is None
    assert rows[fid_b].superseded_by is None


def test_agent_can_exclude_some_candidates_from_cluster():
    """If 3 findings are candidates but the agent only puts 2 in the
    cluster, the third stays independent."""
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="art")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="art again")
    fid_c = _force_unique_finding(finding_type="wiki_contradiction",
                                   reason="grades — unrelated")

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b],  # excludes fid_c
        "root_question": "Did Annika stop art?",
        "reason": "a and b share root; c is unrelated",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")

    with _patch_agent(verdict):
        result = run_cluster_resolve(ctx)

    assert result["findings_superseded"] == 1
    session = get_session()
    try:
        rows = {r.id: r for r in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()
    assert rows[fid_a].superseded_by is None
    assert rows[fid_b].superseded_by == fid_a
    assert rows[fid_c].superseded_by is None  # excluded; remains independent


def test_get_findings_default_hides_superseded_and_stamps_cluster_size():
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction", reason="r1")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction", reason="r2")
    fid_c = _force_unique_finding(finding_type="wiki_contradiction", reason="r3")

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b, fid_c],
        "root_question": "Q?",
        "reason": "x",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")
    with _patch_agent(verdict):
        run_cluster_resolve(ctx)

    findings_default = get_findings(status="pending", limit=100)
    ids = [f["id"] for f in findings_default]
    assert fid_a in ids
    assert fid_b not in ids  # superseded
    assert fid_c not in ids  # superseded

    lead_dict = next(f for f in findings_default if f["id"] == fid_a)
    assert lead_dict["cluster_size"] == 3
    assert lead_dict["cluster_root_question"] == "Q?"

    # include_superseded=True returns all 3
    findings_all = get_findings(status="pending", include_superseded=True, limit=100)
    ids_all = [f["id"] for f in findings_all]
    assert {fid_a, fid_b, fid_c}.issubset(ids_all)


def test_summary_counts_only_count_leads_not_siblings():
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction", reason="r1")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction", reason="r2")
    fid_c = _force_unique_finding(finding_type="wiki_contradiction", reason="r3")

    pre = get_summary_counts()
    assert pre["by_type"]["wiki_contradiction"]["pending"] == 3

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b, fid_c],
        "root_question": "Q?",
        "reason": "x",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")
    with _patch_agent(verdict):
        run_cluster_resolve(ctx)

    post = get_summary_counts()
    # Only the lead is counted now.
    assert post["by_type"]["wiki_contradiction"]["pending"] == 1
    assert post["total_pending"] == 1


def test_cascade_set_status_lead_resolves_siblings():
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction", reason="r1")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction", reason="r2")
    fid_c = _force_unique_finding(finding_type="wiki_contradiction", reason="r3")

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b, fid_c],
        "root_question": "Q?",
        "reason": "x",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")
    with _patch_agent(verdict):
        run_cluster_resolve(ctx)

    cascaded = set_status(fid_a, "executed", executed_by="test")
    assert cascaded == 2

    session = get_session()
    try:
        rows = {r.id: r for r in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()
    assert rows[fid_a].status == "executed"
    assert rows[fid_b].status == "executed"
    assert rows[fid_c].status == "executed"
    assert rows[fid_b].executed_by == "test"  # cascade carries lead's executed_by
    assert "Cascaded from cluster lead" in (rows[fid_b].execution_notes or "")


def test_cascade_skips_non_pending_siblings():
    """If a sibling was somehow already resolved, cascade leaves it alone."""
    _wipe_findings()
    fid_a = _force_unique_finding(finding_type="wiki_contradiction", reason="r1")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction", reason="r2")

    verdict = {
        "is_same_root": True,
        "lead_finding_id": fid_a,
        "member_finding_ids": [fid_a, fid_b],
        "root_question": "Q?",
        "reason": "x",
    }
    ctx = PipelineContext.for_date(pipeline_id="test_cluster")
    with _patch_agent(verdict):
        run_cluster_resolve(ctx)

    # Manually flip sibling to rejected first (not via cascade)
    set_status(fid_b, "rejected", executed_by="manual", cascade_to_siblings=False)

    # Now cascade should NOT re-flip the rejected one
    cascaded = set_status(fid_a, "executed", executed_by="test")
    assert cascaded == 0  # b was no longer pending

    session = get_session()
    try:
        rows = {r.id: r for r in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()
    assert rows[fid_a].status == "executed"
    assert rows[fid_b].status == "rejected"  # untouched


def test_no_candidates_means_no_agent_call():
    """When nothing qualifies, the pipeline returns empty stats and never
    creates an agent."""
    _wipe_findings()
    _make_finding(reason="lonely")

    # Patch should NEVER be called — verify by raising in side_effect
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("agent must not be created when no candidates"),
    ):
        ctx = PipelineContext.for_date(pipeline_id="test_cluster")
        result = run_cluster_resolve(ctx)
    assert result == {
        "candidates_examined": 0,
        "clusters_confirmed": 0,
        "findings_superseded": 0,
    }


def test_clusters_across_finding_types_share_anchor():
    """A primary node with mixed finding types (e.g. duplicate_node +
    wiki_contradiction) is still a candidate — the anchor is the node,
    not the type. The agent decides whether they share a root."""
    _wipe_findings()
    fid_a = _make_finding(finding_type="duplicate_node",
                          secondary_node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                          reason="dup")
    fid_b = _force_unique_finding(finding_type="wiki_contradiction", reason="contra")

    candidates = _build_candidate_clusters()
    assert len(candidates) == 1
    cluster = list(candidates.values())[0]
    assert {f["id"] for f in cluster} == {fid_a, fid_b}
