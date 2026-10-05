"""A failed call stops the KG maintenance run (owner, 2026-10-05).

"If any call fails in KG, we should treat that as a stop processing." Each
test drives one maintenance site into a failed LLM / embedding / Chroma / DB
call (or an answer still incomplete after one correction) and pins that the
run raises, writes no degraded result, leaves the remaining work pending,
and counts the item in the failure ledger where the queue has one.

Uses the kg conftest (isolated test DB, fresh tables per test). Agents and
Chroma are fakes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg.failure_ledger import KGItemFailure
from app.assistant.ServiceLocator.service_locator import DI
from app.models.base import get_session

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _mk_node(label, node_type="Entity", **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _node_exists(nid):
    session = get_session()
    try:
        return session.get(Node, nid) is not None
    finally:
        session.close()


def _failures(kind, item_id):
    session = get_session()
    try:
        row = session.get(KGItemFailure, (kind, str(item_id)))
        return row.failures if row else 0
    finally:
        session.close()


def _findings(finding_type):
    session = get_session()
    try:
        return session.query(KGMaintenanceFinding).filter_by(finding_type=finding_type).all()
    finally:
        session.close()


def _ctx():
    return SimpleNamespace(run_id="test-run", pipeline_id="kg_maintenance_pipeline")


# ── pipeline: a failed step stops the remaining steps ────────────────────


def test_failed_step_stops_the_remaining_steps(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import (
        step_context_embedding_backfill, step_lifecycle_gc, step_orphan_scan,
    )
    from app.assistant.pipelines.kg_maintenance_pipeline.pipeline import KGMaintenancePipeline

    ran = []
    monkeypatch.setattr(step_lifecycle_gc, "run", lambda ctx: ran.append("lifecycle_gc") or {})

    def broken(ctx):
        ran.append("orphan_scan")
        raise RuntimeError("db locked")

    monkeypatch.setattr(step_orphan_scan, "run", broken)
    monkeypatch.setattr(step_context_embedding_backfill, "run", lambda ctx: ran.append("backfill") or {})

    with pytest.raises(RuntimeError, match="db locked"):
        KGMaintenancePipeline().run(only_steps=["lifecycle_gc", "orphan_scan", "context_embedding_backfill"])
    assert ran == ["lifecycle_gc", "orphan_scan"]


# ── execute_findings: a failed merger call means no merge ─────────────────


def _dup_finding(a, b):
    return {"id": "f1", "finding_type": "duplicate_node", "primary_node_id": a, "secondary_node_id": b}


@pytest.mark.parametrize("merger", ["raises", "empty"])
def test_failed_merger_call_merges_nothing(monkeypatch, merger):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_execute_findings as ex

    a, b = _mk_node("Alex"), _mk_node("Alex K")

    class _Merger:
        def action_handler(self, msg):
            if merger == "raises":
                raise RuntimeError("LLM down")
            return SimpleNamespace(data=None)

    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: _Merger())
    monkeypatch.setattr("app.assistant.scope.loader.load_scope_for_source", lambda **k: None)
    with pytest.raises(RuntimeError):
        ex.execute_single_finding(_dup_finding(a, b))
    assert _node_exists(a) and _node_exists(b)


def test_execute_approved_stops_at_the_first_failure(monkeypatch):
    from flask import Flask

    import app.routes.kg_maintenance as route
    from app.assistant.pipelines.kg_maintenance_pipeline import step_execute_findings as ex

    approved = [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}]
    executed, statuses = [], {}

    def fake_execute(finding):
        executed.append(finding["id"])
        if finding["id"] == "f2":
            raise RuntimeError("merger down")
        return {"executed": True, "detail": "ok"}

    monkeypatch.setattr(ex, "execute_single_finding", fake_execute)
    monkeypatch.setattr(route, "get_findings", lambda **k: approved)
    monkeypatch.setattr(route, "set_status", lambda fid, status, **k: statuses.__setitem__(fid, status))

    app = Flask(__name__)
    app.register_blueprint(route.kg_maintenance_bp)
    resp = app.test_client().post("/kg-maintenance/api/execute_approved")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["stopped_at"] == "f2" and body["executed"] == 1
    assert executed == ["f1", "f2"]                   # f3 stays approved
    assert statuses == {"f1": "executed", "f2": "execute_error"}


# ── role_succession_scan: a malformed verdict gets one correction ────────


def _possessive_with_evidence(label="Kid's School"):
    nid = _mk_node(label)
    session = get_session()
    try:
        for i in range(3):
            session.add(KGNodeEvidence(id=str(uuid.uuid4()), node_id=nid, derived_sentence=f"fact {i}",
                                       message_timestamp=T0 + timedelta(days=i)))
        session.commit()
    finally:
        session.close()
    return nid


def test_malformed_succession_verdict_is_corrected_once_then_raises(monkeypatch):
    import app.assistant.pipelines.kg_maintenance_pipeline.step_role_succession_scan as mod

    nid = _possessive_with_evidence()
    seen = []

    def judge(agent_input):
        seen.append(dict(agent_input))
        return {"verdict": "succession", "confidence": "high", "reasoning": "changed"}  # no split_date

    monkeypatch.setattr(mod, "_run_judge", judge)
    with pytest.raises(ValueError, match="after one correction"):
        mod.run(_ctx())
    assert len(seen) == 2 and "split_date" in seen[1]["correction"]
    assert _findings("referent_succession") == []     # no cooldown stamp
    assert _failures(mod.FAILURE_KIND, nid) == 1


def test_corrected_succession_verdict_is_used(monkeypatch):
    import app.assistant.pipelines.kg_maintenance_pipeline.step_role_succession_scan as mod

    _possessive_with_evidence()
    answers = [{"verdict": "maybe", "confidence": "high", "reasoning": "?"},
               {"verdict": "stable", "confidence": "medium", "reasoning": "one school"}]
    monkeypatch.setattr(mod, "_run_judge", lambda agent_input: answers.pop(0))
    result = mod.run(_ctx())
    assert result["stable"] == 1 and answers == []


# ── wiki_inference: a failed finding write leaves the page unexamined ────


def test_failed_finding_write_leaves_the_page_unexamined(monkeypatch, tmp_path):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_wiki_inference as mod

    nid = _mk_node("Subject Person")
    (tmp_path / "prose").mkdir()
    page = tmp_path / "prose" / "Subject Person.md"
    page.write_text("Subject Person. " * 50, encoding="utf-8")
    monkeypatch.setenv("EMI_WIKI_DIR", str(tmp_path))

    class _Investigator:
        def action_handler(self, msg):
            return SimpleNamespace(data={"sentences": [{
                "sentence": "Subject Person lives in a town.", "confidence": 0.9, "not_already_in_kg": True}]})

    monkeypatch.setattr(mod.DI.agent_factory, "create_agent", lambda name: _Investigator())

    def broken_upsert(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mod, "upsert_finding", broken_upsert)
    with pytest.raises(RuntimeError, match="database is locked"):
        mod.run(_ctx())
    assert not page.with_suffix(".wiki_inference.json").exists()
    assert _failures(mod.FAILURE_KIND, nid) == 1


# ── edge_canon_curation: nothing is applied when a batch fails ───────────


def _seed_raw_predicate(pred="hangs_out_with", n=3):
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge
    session = get_session()
    try:
        for i in range(n):
            a, b = str(uuid.uuid4()), str(uuid.uuid4())
            session.add(Node(id=a, label=f"P{i}a", node_type="Entity"))
            session.add(Node(id=b, label=f"P{i}b", node_type="Entity"))
            session.add(Edge(id=str(uuid.uuid4()), source_id=a, target_id=b, relationship_type=pred,
                             sentence=f"P{i}a {pred} P{i}b."))
        session.commit()
    finally:
        session.close()


def _edge_types():
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge
    session = get_session()
    try:
        return {r[0] for r in session.query(Edge.relationship_type).all()}
    finally:
        session.close()


def test_curator_agent_missing_raises(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_edge_canon_curation as mod

    _seed_raw_predicate()
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: None)
    with pytest.raises(RuntimeError, match="edge_canon_curator"):
        mod.run(_ctx())


def test_incomplete_curator_answer_is_corrected_once_then_raises(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_edge_canon_curation as mod

    _seed_raw_predicate()
    seen = []

    class _Curator:
        def action_handler(self, msg):
            seen.append(msg.agent_input["information"])
            return SimpleNamespace(data={"verdicts": [
                {"predicate": "hangs_out_with", "verdict": "variant_of", "canonical_target": "no_such_canon",
                 "reason": "x"}]})

    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: _Curator())
    monkeypatch.setattr("app.assistant.scope.loader.load_scope_for_source", lambda **k: None)
    with pytest.raises(RuntimeError, match="after one correction"):
        mod.run(_ctx())
    assert seen[0] == "" and "no_such_canon" in seen[1]
    assert "hangs_out_with" in _edge_types()          # nothing applied


# ── cluster_resolve ──────────────────────────────────────────────────────


def _two_findings_on(node_id):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    session = get_session()
    try:
        for i, fid in enumerate(ids):
            session.add(KGMaintenanceFinding(
                id=fid, finding_type="wiki_contradiction", primary_node_id=node_id,
                suggested_action="review", reason=f"reason {i}", status="pending", agent_name="test",
            ))
        session.commit()
    finally:
        session.close()
    return ids


def test_cluster_call_failure_stops_and_counts(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_cluster_resolve as mod

    nid = _mk_node("Clustered")
    _two_findings_on(nid)

    class _Broken:
        def action_handler(self, msg):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(mod.DI.agent_factory, "create_agent", lambda name: _Broken())
    with pytest.raises(RuntimeError, match="LLM down"):
        mod.run(_ctx())
    assert _failures(mod.FAILURE_KIND, nid) == 1


def test_same_root_without_question_is_corrected_once(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_cluster_resolve as mod

    nid = _mk_node("Clustered")
    fa, fb = _two_findings_on(nid)
    answers = [
        {"is_same_root": True, "lead_finding_id": fa, "member_finding_ids": [fa, fb], "root_question": ""},
        {"is_same_root": True, "lead_finding_id": fa, "member_finding_ids": [fa, fb],
         "root_question": "Which is right?", "reason": "same fact"},
    ]
    seen = []

    class _Resolver:
        def action_handler(self, msg):
            seen.append(msg.agent_input.get("correction"))
            return SimpleNamespace(data=answers.pop(0))

    monkeypatch.setattr(mod.DI.agent_factory, "create_agent", lambda name: _Resolver())
    result = mod.run(_ctx())
    assert result["clusters_confirmed"] == 1
    assert seen[0] is None and "root_question is empty" in seen[1]


# ── context_embedding_backfill ───────────────────────────────────────────


def test_failed_embedding_stops_the_backfill(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_context_embedding_backfill as mod

    first = _mk_node("First", original_sentence="First is a thing.")
    second = _mk_node("Second", original_sentence="Second is a thing.")
    stored = []

    class _Collection:
        def get(self, ids, include):
            return {"ids": []}

    class _Chroma:
        node_context_collection = _Collection()
        node_collection = _Collection()

        def store_node_context_embedding(self, node_id, text, emb):
            stored.append(node_id)

        def store_node_embedding(self, node_id, text, emb):
            stored.append(node_id)

    import app.assistant.embeddings.embedder as embedder
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: _Chroma())

    def broken_embed(text):
        raise RuntimeError("embedder down")

    monkeypatch.setattr(embedder, "embed_text", broken_embed)
    with pytest.raises(RuntimeError, match="embedder down"):
        mod.run(_ctx())
    assert stored == []
    # The first node tried is counted; the run stopped before the second.
    assert sorted(_failures(mod.CONTEXT_KIND, n) for n in (first, second)) == [0, 1]


# ── missing_dates_scan / disambiguation_scan ─────────────────────────────


def test_missing_dates_scan_requires_the_primary_user_name(monkeypatch):
    from app.assistant.pipelines.kg_maintenance_pipeline import step_missing_dates_scan as mod

    def unconfigured():
        raise RuntimeError("Primary user name is required but not configured.")

    monkeypatch.setattr(mod, "get_required_primary_user_name", unconfigured)
    with pytest.raises(RuntimeError, match="Primary user name"):
        mod.run(_ctx())


def test_failed_temporal_drain_stops_the_scan(monkeypatch):
    from app.assistant.kg.disambiguation import create_disambiguation
    from app.assistant.pipelines.kg_maintenance_pipeline import step_disambiguation_scan as mod

    session = get_session()
    try:
        create_disambiguation(session, label="Alex", reason="two people")
        session.commit()
    finally:
        session.close()
    _mk_node("Alex")
    _mk_node("Alex")

    def broken_drain(session, node_id, label):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mod, "temporal_drain", broken_drain)
    with pytest.raises(RuntimeError, match="database is locked"):
        mod.run(_ctx())
    assert _findings("disambiguation_backlog") == []
