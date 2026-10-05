"""P1.1–P1.4 acceptance tests (audit 2026-06-09).

P1.1 — detector output validated against the candidate pair: actions
naming foreign/echoed/hallucinated ids are dropped; duplicate_node
findings with non-live ids are refused at upsert.

P1.2 — adjudication-aware stamping: detector-agent-None aborts the run
loudly; per-pair detector failures keep both nodes unstamped; renames,
alias/category edits, and merges reset last_dupe_scanned_at.

P1.4 — verdict lifecycle: triage reasons persisted per-pair; nano-tier
'distinct' verdicts expire from the scan loader after 90d (investigator
verdicts don't); one active verdict per (pair, type) — newer supersedes;
investigate_one refuses verdicts naming non-live node ids.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.kg_maintenance.verdict_store import (
    NANO_TRIAGE_DECIDED_BY,
    load_distinct_pairs,
    record_distinct_pairs_bulk,
    record_verdict,
)
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────


def _mk_node(label: str, *, stamped: bool = False, node_type: str = "Entity") -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(
            id=nid, label=label, node_type=node_type,
            last_dupe_scanned_at=NOW if stamped else None,
        ))
        session.commit()
    finally:
        session.close()
    return nid


def _node_field(nid: str, field: str):
    session = get_session()
    try:
        return getattr(session.query(Node).filter_by(id=nid).one(), field)
    finally:
        session.close()


def _mk_verdict(a: str, b: str | None, *, decided_by: str = "test",
                created_days_ago: int = 0, verdict_type: str = "distinct") -> str:
    session = get_session()
    try:
        v = KGNodeVerdict(
            node_id_a=min(a, b) if b else a,
            node_id_b=max(a, b) if b else None,
            verdict_type=verdict_type,
            memo="test verdict",
            decided_by=decided_by,
            created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        )
        session.add(v)
        session.commit()
        return v.id
    finally:
        session.close()


def _active_verdicts(a: str, b: str | None = None) -> list:
    session = get_session()
    try:
        q = session.query(KGNodeVerdict).filter(
            KGNodeVerdict.node_id_a.in_([x for x in (a, b) if x])
            | KGNodeVerdict.node_id_b.in_([x for x in (a, b) if x])
        )
        rows = q.all()
        for r in rows:
            session.expunge(r)
        return rows
    finally:
        session.close()


def _call_tool(tool_name: str, **arguments):
    msg = ToolMessage(tool_name=tool_name, tool_data={"tool_name": tool_name, "arguments": arguments})
    return KGMutatorTool().execute(msg)


# ── P1.1: detector output validation ─────────────────────────────────────


def test_write_findings_drops_foreign_and_untagged_actions():
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        _write_findings,
    )

    a = _mk_node("Alex")
    b = _mk_node("Aleks")
    foreign = _mk_node("Bystander")

    actions = [
        # Valid: ids are exactly the source pair.
        {"merge": [a, b], "labels": ["Alex", "Aleks"], "reason": "same person",
         "_source_pair": [a, b]},
        # Foreign id smuggled in (transposition / echoed example).
        {"merge": [a, foreign], "labels": ["Alex", "Bystander"], "reason": "x",
         "_source_pair": [a, b]},
        # Untagged action (no provenance) — dropped.
        {"merge": [a, b], "labels": ["Alex", "Aleks"], "reason": "x"},
    ]
    created = _write_findings(actions, pipeline_run_id="test-run")
    assert created == 1

    session = get_session()
    try:
        rows = session.query(KGMaintenanceFinding).filter_by(
            finding_type="duplicate_node").all()
        assert len(rows) == 1
        assert {rows[0].primary_node_id, rows[0].secondary_node_id} == {a, b}
    finally:
        session.close()


def test_upsert_refuses_duplicate_node_finding_with_dead_ids():
    live = _mk_node("Real")
    dead = str(uuid.uuid4())

    fid, created = upsert_finding(
        finding_type="duplicate_node",
        primary_node_id=live,
        secondary_node_id=dead,
        suggested_action="merge",
        agent_name="test",
    )
    assert created is False and fid == ""

    # Non-duplicate types are unaffected (their producers vouch differently).
    fid2, created2 = upsert_finding(
        finding_type="orphan_node",
        primary_node_id=dead,
        suggested_action="review",
        agent_name="test",
    )
    assert created2 is True


# ── P1.2: detector failures and the stamp ────────────────────────────────


class _UncertainTriage:
    """Answers 'uncertain' for every pair it is shown (all pass to the detector)."""
    def action_handler(self, msg):
        import json
        briefs = json.loads(msg.agent_input["pairs_batch"])
        return SimpleNamespace(data={"pairs": [
            {"pair_index": b["pair_index"], "verdict": "uncertain", "reason": ""} for b in briefs]})


def _fake_factories(monkeypatch, *, detector_agent, triage_agent=None):
    """Triage passes every pair through unless a triage fake is given;
    detector uses the given fake. Mirrors DI.agent_factory.create_agent's
    dispatch-by-name."""
    triage_agent = triage_agent or _UncertainTriage()

    def fake_create(name):
        if "duplicate_triage" in name:
            return triage_agent
        return detector_agent
    monkeypatch.setattr(DI.agent_factory, "create_agent", fake_create)


def _descs(*ids):
    return {n: {"node_id": n, "label": n, "node_type": "Entity",
                "description": "", "aliases": [], "category": "",
                "semantic_label": "", "original_sentence": "",
                "start_date": None, "end_date": None,
                "edge_sentences": [], "edge_count": 0, "neighborhood": []}
            for n in ids}


def _ledger_failures(item_id):
    from app.assistant.kg.failure_ledger import KGItemFailure
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import PAIR_FAILURE_KIND
    session = get_session()
    try:
        row = session.get(KGItemFailure, (PAIR_FAILURE_KIND, item_id))
        return row.failures if row else 0
    finally:
        session.close()


def test_detector_agent_none_aborts_loudly(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        _confirm_pairs_with_llm,
    )

    _fake_factories(monkeypatch, detector_agent=None)
    a, b = _mk_node("A"), _mk_node("B")

    with pytest.raises(RuntimeError, match="duplicate_detector"):
        _confirm_pairs_with_llm([(a, b, "tier1")], _descs(a, b), None)


def test_detector_failure_stops_and_counts_the_pair(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        _confirm_pairs_with_llm, _pair_item_id,
    )

    a, b = _mk_node("A"), _mk_node("B")
    c, d = _mk_node("C"), _mk_node("D")
    calls = {"n": 0}

    class _Detector:
        def action_handler(self, msg):
            calls["n"] += 1
            raise RuntimeError("LLM hiccup")

    _fake_factories(monkeypatch, detector_agent=_Detector())

    with pytest.raises(RuntimeError, match="LLM hiccup"):
        _confirm_pairs_with_llm([(a, b, "tier1"), (c, d, "tier1")], _descs(a, b, c, d), None)
    assert calls["n"] == 1                        # the second pair never ran
    assert _ledger_failures(_pair_item_id(a, b)) == 1


def test_detector_success_tags_actions_and_clears_the_ledger(monkeypatch):
    from app.assistant.kg import failure_ledger
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        PAIR_FAILURE_KIND, _confirm_pairs_with_llm, _pair_item_id,
    )

    c, d = _mk_node("C"), _mk_node("D")
    failure_ledger.record_failure(PAIR_FAILURE_KIND, _pair_item_id(c, d), RuntimeError("earlier"))

    class _Detector:
        def action_handler(self, msg):
            return SimpleNamespace(data={"merge_actions": [
                {"merge": [c, d], "labels": ["C", "D"], "reason": "same"},
            ]})

    _fake_factories(monkeypatch, detector_agent=_Detector())
    actions, failed = _confirm_pairs_with_llm([(c, d, "tier1")], _descs(c, d), None)
    assert failed == set()
    assert actions[0]["_source_pair"] == [c, d]   # provenance tag attached
    assert _ledger_failures(_pair_item_id(c, d)) == 0


def test_failed_triage_batch_stops_the_run(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        _confirm_pairs_with_llm,
    )

    class _BrokenTriage:
        def action_handler(self, msg):
            raise RuntimeError("context_length_exceeded")

    a, b = _mk_node("A"), _mk_node("B")
    _fake_factories(monkeypatch, detector_agent=None, triage_agent=_BrokenTriage())
    with pytest.raises(RuntimeError, match="context_length_exceeded"):
        _confirm_pairs_with_llm([(a, b, "tier1")], _descs(a, b), None)


def test_incomplete_triage_gets_one_correction_then_raises(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan import (
        _confirm_pairs_with_llm,
    )

    seen = []

    class _HalfTriage:
        def action_handler(self, msg):
            seen.append(dict(msg.agent_input))
            return SimpleNamespace(data={"pairs": [
                {"pair_index": 1, "verdict": "distinct", "reason": "different"}]})

    a, b, c, d = (_mk_node(x) for x in "ABCD")
    _fake_factories(monkeypatch, detector_agent=None, triage_agent=_HalfTriage())
    with pytest.raises(RuntimeError, match="after one correction"):
        _confirm_pairs_with_llm([(a, b, "tier1"), (c, d, "tier1")], _descs(a, b, c, d), None)
    assert "correction" not in seen[0]
    assert "no verdict for pair_index [2]" in seen[1]["correction"]
    assert load_distinct_pairs() == set()         # nothing recorded from the broken batch


def test_set_aside_pair_is_skipped_and_its_nodes_stay_unstamped(monkeypatch):
    from app.assistant.kg import failure_ledger
    from app.assistant.pipelines.kg_maintenance_pipeline import step_duplicate_scan as scan

    a, b = _mk_node("Same Name"), _mk_node("Same Name")
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(scan.PAIR_FAILURE_KIND, scan._pair_item_id(a, b), RuntimeError("refused"))
    monkeypatch.setattr(scan, "_embedding_similarity_pairs", lambda ids: [])
    monkeypatch.setattr(scan, "_identity_similarity_pairs", lambda descs: [])

    descs = _descs(a, b)
    for d in descs.values():
        d["label"] = "Same Name"                  # a Tier 1 pair
    pairs, _ctx, unstamped = scan._build_candidate_pairs(descs, {a, b})
    assert pairs == [] and unstamped == {a, b}


def test_tier5b_chroma_failure_raises(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_duplicate_scan as scan

    class _Broken:
        @property
        def node_identity_collection(self):
            raise RuntimeError("chroma down")

    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: _Broken())
    with pytest.raises(RuntimeError, match="chroma down"):
        scan._identity_similarity_pairs(_descs("a", "b"))


def test_run_stamps_nothing_when_a_call_fails(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_duplicate_scan as scan

    a, b = _mk_node("Twin"), _mk_node("Twin")
    monkeypatch.setattr(scan, "_embedding_similarity_pairs", lambda ids: [])
    monkeypatch.setattr(scan, "_identity_similarity_pairs", lambda descs: [])
    monkeypatch.setattr(scan, "load_scope_for_source", lambda **k: None)

    class _Detector:
        def action_handler(self, msg):
            raise RuntimeError("LLM down")

    _fake_factories(monkeypatch, detector_agent=_Detector())
    with pytest.raises(RuntimeError, match="LLM down"):
        scan.run(SimpleNamespace(run_id="r1", pipeline_id="kg_maintenance_pipeline"))
    assert _node_field(a, "last_dupe_scanned_at") is None
    assert _node_field(b, "last_dupe_scanned_at") is None


def test_rename_resets_stamp_and_supersedes_verdicts():
    nid = _mk_node("Old Name", stamped=True)
    other = _mk_node("Other")
    _mk_verdict(nid, other, decided_by="agent:kg_investigation")

    result = _call_tool("kg_rename_label", node_id=nid,
                        new_label="New Name", reason="test rename")
    assert result.data.get("ok") is True, result.content
    assert _node_field(nid, "last_dupe_scanned_at") is None

    verdicts = _active_verdicts(nid)
    assert all(v.superseded_at is not None for v in verdicts)
    assert any("node_renamed" in (v.superseded_reason or "") for v in verdicts)


def test_alias_update_resets_stamp_and_supersedes():
    nid = _mk_node("Aliased", stamped=True)
    _mk_verdict(nid, None, verdict_type="verified")

    result = _call_tool("kg_update_node_field", node_id=nid, field="aliases",
                        value=["Ali"], list_op="add", reason="test alias add")
    assert result.data.get("ok") is True, result.content
    assert _node_field(nid, "last_dupe_scanned_at") is None
    assert all(v.superseded_at is not None for v in _active_verdicts(nid))


def test_category_update_resets_stamp_only():
    nid = _mk_node("Cat", stamped=True)
    vid = _mk_verdict(nid, None, verdict_type="verified")

    result = _call_tool("kg_update_node_field", node_id=nid, field="category",
                        value="person", reason="test category")
    assert result.data.get("ok") is True, result.content
    assert _node_field(nid, "last_dupe_scanned_at") is None
    # Category isn't a name change — verdicts stay active.
    assert all(v.superseded_at is None for v in _active_verdicts(nid))


def _fake_chroma(monkeypatch):
    """A merge removes the folded node's Chroma vectors and, since 2026-10-05, raises when it cannot; tests never open
    the real index."""
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    fake = SimpleNamespace(delete_node_embedding=lambda nid: None, delete_node_context_embedding=lambda nid: None,
                           delete_node_identity_embedding=lambda nid: None)
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake)


def test_merge_resets_survivor_stamp(monkeypatch):
    _fake_chroma(monkeypatch)
    keep = _mk_node("Keeper", stamped=True)
    fold = _mk_node("Folder")

    result = _call_tool("kg_merge_nodes", keep_id=keep, fold_id=fold,
                        reason="test merge")
    assert result.data.get("ok") is True, result.content
    assert _node_field(keep, "last_dupe_scanned_at") is None


def test_merge_nodes_in_session_resets_winner_stamp(monkeypatch):
    from app.assistant.kg_core.kg_utils.node_merge import merge_nodes_in_session
    _fake_chroma(monkeypatch)

    winner = _mk_node("Winner", stamped=True)
    loser = _mk_node("Loser")
    session = get_session()
    try:
        w = session.query(Node).filter_by(id=winner).one()
        l = session.query(Node).filter_by(id=loser).one()
        merge_nodes_in_session(session, loser_node=l, winner_node=w,
                               merge_actor="test")
        session.commit()
    finally:
        session.close()
    assert _node_field(winner, "last_dupe_scanned_at") is None


# ── P1.4: verdict lifecycle ──────────────────────────────────────────────


def test_bulk_distinct_persists_per_pair_reasoning():
    a, b = sorted([_mk_node("A"), _mk_node("B")])
    n = record_distinct_pairs_bulk(
        [(a, b, "different people per edge sentences")],
        decided_by=NANO_TRIAGE_DECIDED_BY,
        memo="nano triage scored as distinct",
    )
    assert n == 1
    session = get_session()
    try:
        v = session.query(KGNodeVerdict).filter_by(node_id_a=a, node_id_b=b).one()
        assert v.reasoning == "different people per edge sentences"
    finally:
        session.close()


def test_nano_verdicts_expire_from_scan_loader():
    a1, b1 = sorted([_mk_node("N1"), _mk_node("N2")])
    a2, b2 = sorted([_mk_node("N3"), _mk_node("N4")])
    a3, b3 = sorted([_mk_node("N5"), _mk_node("N6")])

    _mk_verdict(a1, b1, decided_by=NANO_TRIAGE_DECIDED_BY, created_days_ago=120)
    _mk_verdict(a2, b2, decided_by=NANO_TRIAGE_DECIDED_BY, created_days_ago=5)
    _mk_verdict(a3, b3, decided_by="agent:kg_investigation", created_days_ago=120)

    pairs = load_distinct_pairs()
    assert (a1, b1) not in pairs          # nano + stale → expired
    assert (a2, b2) in pairs              # nano + fresh → active
    assert (a3, b3) in pairs              # investigator → permanent


def test_record_verdict_supersedes_same_pair_and_type():
    a, b = sorted([_mk_node("P1"), _mk_node("P2")])
    v1 = record_verdict(verdict_type="distinct", memo="first call",
                        node_ids=[a, b], decided_by="agent:kg_investigation")
    v2 = record_verdict(verdict_type="distinct", memo="second call",
                        node_ids=[a, b], decided_by="agent:kg_investigation")
    assert v1 and v2 and v1 != v2

    session = get_session()
    try:
        rows = session.query(KGNodeVerdict).filter_by(
            node_id_a=a, node_id_b=b, verdict_type="distinct").all()
        active = [r for r in rows if r.superseded_at is None]
        assert len(rows) == 2
        assert len(active) == 1
        assert active[0].id == v2
    finally:
        session.close()


def test_investigate_one_refuses_dead_verdict_ids(monkeypatch):
    from app.assistant.kg_investigator import finding_processor

    node = _mk_node("Subject")
    fid, _ = upsert_finding(
        finding_type="orphan_node", primary_node_id=node,
        suggested_action="review", agent_name="test",
    )
    dead_id = str(uuid.uuid4())
    report = {
        "take_action": False,
        "verdict_type": "verified",
        "verdict_memo": f"data on {dead_id[:8]} is fine",
        "verdict_node_ids": [dead_id],
        "recommendation": "all good",
    }

    fake_mgr = SimpleNamespace(blackboard=SimpleNamespace(get_messages=lambda: []))
    monkeypatch.setattr(DI.multi_agent_manager_factory, "create_manager",
                        lambda name: fake_mgr)
    monkeypatch.setattr(DI.manager_invoker, "invoke",
                        lambda mgr, msg: ToolResult(result_type="final_answer", content="done"))
    monkeypatch.setattr(finding_processor, "_extract_report_from_audit",
                        lambda bb: report)

    result = finding_processor.investigate_one(fid)
    assert result["status"] == "escalated"
    assert result["reason"] == "verdict_node_ids_not_live"

    session = get_session()
    try:
        f = session.query(KGMaintenanceFinding).filter_by(id=fid).one()
        assert f.status == "escalated"
        n_verdicts = session.query(KGNodeVerdict).filter_by(
            node_id_a=dead_id).count()
        assert n_verdicts == 0            # nothing durable about a ghost id
    finally:
        session.close()
