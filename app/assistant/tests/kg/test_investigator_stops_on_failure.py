"""A failed call in the KG investigator's helpers stops processing (owner, 2026-10-05).

- duplicate cluster drain: a node the resolver leaves out is never taken as
  'distinct' (the resolver's validate.py; the agent corrects once and
  raises if still incomplete); the first failing cluster stops
  the drain and is counted in the failure ledger; a cluster's merges commit
  together or not at all
- finding brief: a failed read raises instead of a thinner brief
- consistency critic: a failed investigation or finding write propagates

Uses the kg conftest (isolated test DB, fresh tables per test). Agents and
Chroma are fakes.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.kg import failure_ledger
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_investigator import duplicate_cluster_drain as drain
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.ServiceLocator.service_locator import DI
from app.models.base import get_session


@pytest.fixture(autouse=True)
def _clean_ledger():
    session = get_session()
    try:
        failure_ledger._ensure(session)
        session.query(failure_ledger.KGItemFailure).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_node(label: str) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Event"))
        session.commit()
    finally:
        session.close()
    return nid


def _dup_finding(a: str, b: str) -> str:
    fid, created = upsert_finding(
        finding_type="duplicate_node", primary_node_id=a, secondary_node_id=b,
        suggested_action="merge", agent_name="test",
    )
    assert created
    return fid


def _statuses() -> dict:
    session = get_session()
    try:
        return {f.id: f.status for f in session.query(KGMaintenanceFinding).all()}
    finally:
        session.close()


def _node_exists(nid: str) -> bool:
    session = get_session()
    try:
        return session.query(Node).filter_by(id=nid).count() == 1
    finally:
        session.close()


def _ledger_failures(kind: str, item_id: str) -> int:
    session = get_session()
    try:
        row = session.get(failure_ledger.KGItemFailure, (kind, item_id))
        return row.failures if row else 0
    finally:
        session.close()


class _Resolver:
    """Fake duplicate_cluster_resolver: answers from a list, records each input."""

    def __init__(self, answers, on_call=None):
        self.answers = list(answers)
        self.inputs = []
        self.on_call = on_call

    def action_handler(self, msg):
        self.inputs.append(dict(msg.agent_input))
        if self.on_call:
            self.on_call()
        return SimpleNamespace(data=self.answers.pop(0))


@pytest.fixture
def resolver(monkeypatch):
    holder = SimpleNamespace(agent=None)
    monkeypatch.setattr(drain, "_resolver_scope", lambda: None)
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: holder.agent)
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda *a, **k: MagicMock())
    return holder


# ── duplicate cluster drain ──────────────────────────────────────────────


def _coverage_rules():
    from app.assistant.agent_registry.agent_registry import AgentRegistry
    from app.assistant.utils.path_utils import get_app_root
    return AgentRegistry()._load_validator(
        get_app_root() / "assistant" / "agents" / "kg_maintenance" / "duplicate_cluster_resolver" / "validate.py")


def test_the_resolver_is_asked_once_with_the_cluster_s_node_ids(resolver):
    a, b, c = _mk_node("Walk"), _mk_node("Walk"), _mk_node("Walk")
    _dup_finding(a, b)
    _dup_finding(b, c)
    [cluster] = drain.build_duplicate_clusters()
    ids = cluster["node_ids"]

    resolver.agent = _Resolver([{"merge_groups": [], "distinct_node_ids": ids}])
    report = drain.resolve_one_cluster(cluster, scope_context=None, dry_run=True)

    assert len(resolver.agent.inputs) == 1
    assert resolver.agent.inputs[0]["node_ids"] == ids and "correction" not in resolver.agent.inputs[0]
    assert report["distinct_count"] == 3


def test_the_coverage_contract_never_takes_an_omitted_node_as_distinct():
    """The contract moved to the resolver's validate.py (2026-11-15); the agent corrects once and raises."""
    rules = _coverage_rules()
    ids = {"node_ids": ["n1", "n2", "n3"]}
    found = {(p.value, p.why, p.structural) for p in rules.validate(
        {"merge_groups": [], "distinct_node_ids": ["n1", "n2", "n2", "invented-id"]}, ids)}
    assert ("n3", "not placed; place every node exactly once, in a merge group or in distinct_node_ids",
            True) in found
    assert ("n2", "placed more than once; place each node exactly once", True) in found
    assert ("invented-id", "it is not an id of this cluster; use only the ids shown", True) in found
    no_dup = rules.validate({"merge_groups": [{"canonical_node_id": "n1", "duplicate_node_ids": []}],
                             "distinct_node_ids": ["n2", "n3"]}, ids)
    assert [(p.item, p.at, p.structural) for p in no_dup] == [("n1", "duplicate_node_ids", False)]
    assert rules.ITEM_KEYS == {"merge_groups": "canonical_node_id"}
    assert rules.validate({"merge_groups": [{"canonical_node_id": "n1", "duplicate_node_ids": ["n2"]}],
                           "distinct_node_ids": ["n3"]}, ids) == []


def test_an_answer_still_refused_after_the_correction_stops_the_drain(resolver):
    from app.assistant.agent_runtime.correction import AgentOutputInvalid, Problem

    a, b, c = _mk_node("Walk"), _mk_node("Walk"), _mk_node("Walk")
    first = [_dup_finding(a, b), _dup_finding(b, c)]
    d, e = _mk_node("Dinner"), _mk_node("Dinner")
    later = _dup_finding(d, e)
    big = drain.build_duplicate_clusters()[0]

    def refused():
        raise AgentOutputInvalid(drain.AGENT_NAME, [Problem(why="not placed", value=a, structural=True)])

    resolver.agent = _Resolver([None], on_call=refused)
    with pytest.raises(ValueError, match="not placed"):
        drain.drain_duplicate_clusters(dry_run=False)

    assert len(resolver.agent.inputs) == 1          # the later cluster never ran
    statuses = _statuses()
    assert all(statuses[f] == "pending" for f in first + [later])
    session = get_session()
    try:
        assert session.query(KGNodeVerdict).count() == 0
    finally:
        session.close()
    assert _ledger_failures(drain.LEDGER_KIND, drain._cluster_key(big)) == 1


def test_a_set_aside_cluster_is_skipped(resolver):
    a, b = _mk_node("Walk"), _mk_node("Walk")
    _dup_finding(a, b)
    [cluster] = drain.build_duplicate_clusters()
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(drain.LEDGER_KIND, drain._cluster_key(cluster), RuntimeError("x"))

    resolver.agent = _Resolver([])
    result = drain.drain_duplicate_clusters(dry_run=True)
    assert result["clusters_processed"] == 0
    assert resolver.agent.inputs == []


def test_a_cluster_merges_together_or_not_at_all(resolver):
    """Group one merges, group two's winner has vanished: the whole cluster
    rolls back and its findings stay pending (never 'distinct')."""
    a, b, d, e = (_mk_node(x) for x in ("Routine", "Routine", "Visit", "Visit"))
    findings = [_dup_finding(a, b), _dup_finding(b, d), _dup_finding(d, e)]
    [cluster] = drain.build_duplicate_clusters()

    def delete_d():
        session = get_session()
        try:
            session.query(Node).filter_by(id=d).delete()
            session.commit()
        finally:
            session.close()

    resolver.agent = _Resolver([{
        "merge_groups": [
            {"canonical_node_id": a, "duplicate_node_ids": [b], "reason": "same routine"},
            {"canonical_node_id": d, "duplicate_node_ids": [e], "reason": "same visit"},
        ],
        "distinct_node_ids": [],
    }], on_call=delete_d)

    with pytest.raises(RuntimeError, match="no longer exists"):
        drain.resolve_one_cluster(cluster, scope_context=None, dry_run=False)

    assert _node_exists(b)                           # group one rolled back
    assert all(_statuses()[f] == "pending" for f in findings)


# ── finding brief ────────────────────────────────────────────────────────


class _BrokenSession:
    def execute(self, *a, **k):
        raise RuntimeError("database is locked")


def test_brief_evidence_reads_raise_instead_of_thinning_the_brief():
    from app.assistant.kg_investigator import finding_brief

    with pytest.raises(RuntimeError, match="database is locked"):
        finding_brief._fetch_node_chat_windows(_BrokenSession(), "n1")
    with pytest.raises(RuntimeError, match="database is locked"):
        finding_brief._fetch_evidence_messages(_BrokenSession(), "n1")


# ── consistency critic ───────────────────────────────────────────────────


@pytest.fixture
def critic(monkeypatch, tmp_path):
    from app.assistant.wiki_generator import consistency_critic as cc

    monkeypatch.setattr(cc, "_read_prose_page", lambda vault, label: {
        "prose": "Prose.", "kg_node_id": "n1", "node_type": "Entity", "path": "p.md",
    })
    monkeypatch.setattr(cc, "_build_ground_truth_block", lambda **kw: "")
    monkeypatch.setattr(cc, "_scope", lambda: None)
    agent = SimpleNamespace(action_handler=lambda msg: SimpleNamespace(data={"findings": [
        {"quoted_text": "x", "summary": "contradiction", "issue_type": "contradiction"},
    ]}))
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: agent)
    return cc


def test_critic_lets_a_failed_investigation_propagate(critic, monkeypatch, tmp_path):
    from app.assistant.kg_investigator import finding_processor

    monkeypatch.setattr(critic, "_save_finding", lambda **kw: "f1")

    def boom(ids, max_to_investigate):
        raise RuntimeError("investigation aborted")
    monkeypatch.setattr(finding_processor, "investigate_findings", boom)

    with pytest.raises(RuntimeError, match="investigation aborted"):
        critic.run_consistency_critic(entity_label="Subject", vault_path=tmp_path,
                                      investigate_immediately=True)


def test_critic_finding_write_failure_raises(critic, monkeypatch, tmp_path):
    def broken_query(*a, **k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(critic, "get_session", lambda: SimpleNamespace(
        query=broken_query, rollback=lambda: None, close=lambda: None,
    ))

    with pytest.raises(RuntimeError, match="disk I/O error"):
        critic.run_consistency_critic(entity_label="Subject", vault_path=tmp_path)
