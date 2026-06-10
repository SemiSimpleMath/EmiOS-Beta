"""P0.2 + P1.5(3) acceptance tests (audit 2026-06-09).

P0.2: kg_merge_nodes leaves nothing orphaned at the fold id — dependent
rows rebound via NODE_ID_REFERENCES, section tags migrated, verdicts
naming fold superseded, Chroma label+context embeddings removed, keep's
empty scalars filled from fold, full-row snapshots in before_json, and a
wrong-survivor guard that refuses backwards merges without force=true.

P1.5(3): the shared merge_nodes_in_session helper (used by the cluster
drain, intelligent_merge_nodes, and step_execute_findings) performs the
same section-tag/verdict/Chroma cleanup, so no merge path mints ghost
vectors.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid

import pytest

from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.database.claim_proposals import (
    ClaimProposal,
    ClaimProposalEdge,
    ClaimProposalNode,
)
from app.assistant.database.kg_merge_log import KGMergeLog
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node, NodeSectionTag
from app.assistant.kg_core.kg_utils.node_merge import merge_nodes_in_session
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.utils.pydantic_classes import ToolMessage
from app.models.base import get_session


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_side_tables():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        session.query(NodeSectionTag).delete()
        session.commit()
    finally:
        session.close()
    yield


class _ChromaRecorder:
    def __init__(self):
        self.deleted = []
        self.deleted_context = []

    def delete_node_embedding(self, node_id):
        self.deleted.append(node_id)

    def delete_node_context_embedding(self, node_id):
        self.deleted_context.append(node_id)


@pytest.fixture
def chroma(monkeypatch):
    rec = _ChromaRecorder()
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: rec)
    return rec


def _call(tool_name: str, **arguments):
    msg = ToolMessage(tool_name=tool_name, tool_data={"tool_name": tool_name, "arguments": arguments})
    return KGMutatorTool().execute(msg)


def _mk_node(label: str, node_type: str = "Entity", **fields) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id: str, target_id: str, rel: str = "related_to") -> str:
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(id=eid, source_id=source_id, target_id=target_id, relationship_type=rel))
        session.commit()
    finally:
        session.close()
    return eid


def _add(row) -> None:
    session = get_session()
    try:
        session.add(row)
        session.commit()
    finally:
        session.close()


def _node_exists(nid: str) -> bool:
    session = get_session()
    try:
        return session.query(Node).filter(Node.id == nid).count() == 1
    finally:
        session.close()


def _get_node(nid: str) -> dict:
    session = get_session()
    try:
        n = session.query(Node).filter(Node.id == nid).one()
        return {
            "description": n.description,
            "category": n.category,
            "semantic_label": n.semantic_label,
            "aliases": list(n.aliases or []),
        }
    finally:
        session.close()


def _count(model, **filters) -> int:
    session = get_session()
    try:
        return session.query(model).filter_by(**filters).count()
    finally:
        session.close()


def _merge_revision_rows() -> list:
    session = get_session()
    try:
        return session.query(KGRevisionLog).filter(KGRevisionLog.op == "merge_nodes").all()
    finally:
        session.close()


# ── P0.2: dependent-row rebind ────────────────────────────────────────────


def test_merge_rebinds_dependent_rows(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks")
    other = _mk_node("Bystander")

    proposal_id = str(uuid.uuid4())
    _add(ClaimProposal(id=proposal_id))
    _add(KGNodeEvidence(node_id=fold, derived_sentence="Aleks likes tea"))
    _add(ClaimProposalNode(proposal_id=proposal_id, label="Aleks", node_type="Entity",
                           resolved_node_id=fold))
    _add(ClaimProposalEdge(proposal_id=proposal_id, source_node_id=fold,
                           target_node_id=other, predicate="knows"))

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="duplicate spelling")
    assert result.data.get("ok") is True, result.content

    # Zero rows in any dependent table still reference fold; all moved to keep.
    assert _count(KGNodeEvidence, node_id=fold) == 0
    assert _count(KGNodeEvidence, node_id=keep) == 1
    assert _count(ClaimProposalNode, resolved_node_id=fold) == 0
    assert _count(ClaimProposalNode, resolved_node_id=keep) == 1
    assert _count(ClaimProposalEdge, source_node_id=fold) == 0
    assert _count(ClaimProposalEdge, source_node_id=keep) == 1

    rebound_tables = {r["table"] for r in result.data["dependent_rebinds"]}
    assert "kg_node_evidence" in rebound_tables
    assert "claim_proposal_node" in rebound_tables
    assert "claim_proposal_edge" in rebound_tables


# ── P0.2: section-tag migration ───────────────────────────────────────────


def test_merge_migrates_section_tags(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks")
    _add(NodeSectionTag(node_id=keep, namespace="wiki", section_name="Family"))
    _add(NodeSectionTag(node_id=fold, namespace="wiki", section_name="Family"))
    _add(NodeSectionTag(node_id=fold, namespace="wiki", section_name="Hobbies"))

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="dupe")
    assert result.data.get("ok") is True, result.content

    assert _count(NodeSectionTag, node_id=fold) == 0
    assert _count(NodeSectionTag, node_id=keep, namespace="wiki", section_name="Family") == 1
    assert _count(NodeSectionTag, node_id=keep, namespace="wiki", section_name="Hobbies") == 1
    assert result.data["section_tags_migrated"] == 1

    rows = _merge_revision_rows()
    assert len(rows) == 1
    # Full fold tag set snapshotted (both the migrated and the cascaded one).
    assert len(rows[0].before_json["fold_section_tags"]) == 2


# ── P0.2: verdict supersede ───────────────────────────────────────────────


def test_merge_supersedes_verdicts_touching_fold(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks")
    other = _mk_node("Bystander")
    _add(KGNodeVerdict(node_id_a=fold, node_id_b=other, verdict_type="distinct",
                       memo="keep apart", decided_by="test"))
    _add(KGNodeVerdict(node_id_a=keep, node_id_b=other, verdict_type="distinct",
                       memo="unrelated", decided_by="test"))

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="dupe")
    assert result.data.get("ok") is True, result.content
    assert result.data["verdicts_superseded"] == 1

    session = get_session()
    try:
        fold_v = session.query(KGNodeVerdict).filter(KGNodeVerdict.node_id_a == fold).one()
        keep_v = session.query(KGNodeVerdict).filter(KGNodeVerdict.node_id_a == keep).one()
    finally:
        session.close()
    assert fold_v.superseded_at is not None
    assert keep in fold_v.superseded_reason
    assert keep_v.superseded_at is None


# ── P0.2: wrong-survivor guard ────────────────────────────────────────────


def test_merge_refuses_when_fold_has_more_edges(chroma):
    keep = _mk_node("Sparse")
    fold = _mk_node("Hub")
    _mk_edge(fold, _mk_node("N1"), "knows")
    _mk_edge(_mk_node("N2"), fold, "mentions")

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="backwards")
    assert result.result_type == "error"
    assert "outranks" in (result.content or "")
    assert "force" in (result.content or "")
    assert _node_exists(fold)
    assert _merge_revision_rows() == []
    assert chroma.deleted == []

    forced = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="intentional", force=True)
    assert forced.data.get("ok") is True, forced.content
    assert not _node_exists(fold)
    assert forced.data["survivor_warning"]


def test_merge_refuses_when_fold_has_higher_pagerank(chroma):
    keep = _mk_node("Minor", pagerank_score=0.1)
    fold = _mk_node("Major", pagerank_score=0.9)

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="backwards")
    assert result.result_type == "error"
    assert "outranks" in (result.content or "")
    assert _node_exists(fold)

    forced = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="intentional", force=True)
    assert forced.data.get("ok") is True, forced.content
    assert not _node_exists(fold)


# ── P0.2: NULL-scalar fill ────────────────────────────────────────────────


def test_merge_fills_empty_keep_fields_never_overwrites(chroma):
    keep = _mk_node("Alex", category="person")
    fold = _mk_node("Aleks", description="desc from fold", category="other",
                    semantic_label="friend")

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="dupe")
    assert result.data.get("ok") is True, result.content

    after = _get_node(keep)
    assert after["description"] == "desc from fold"   # NULL → filled
    assert after["category"] == "person"              # non-NULL → untouched
    assert after["semantic_label"] == "friend"        # NULL → filled
    assert sorted(result.data["filled_fields"]) == ["description", "semantic_label"]


# ── P0.2: full-row snapshots ──────────────────────────────────────────────


def test_merge_snapshot_is_full_row(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks", attributes={"k": "v"}, confidence=0.7, source="test_src")

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="dupe")
    assert result.data.get("ok") is True, result.content

    rows = _merge_revision_rows()
    assert len(rows) == 1
    fold_snap = rows[0].before_json["fold"]
    assert fold_snap["attributes"] == {"k": "v"}
    assert fold_snap["confidence"] == 0.7
    assert fold_snap["source"] == "test_src"
    # Previously-omitted columns are present (value may be NULL).
    for key in ("confidence_tier", "locked_by_user_at", "pagerank_score",
                "observation_count", "first_observed", "last_observed"):
        assert key in fold_snap


# ── P0.2: Chroma cleanup on the mutator path ──────────────────────────────


def test_merge_cleans_fold_chroma_vectors(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks")

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="dupe")
    assert result.data.get("ok") is True, result.content
    assert chroma.deleted == [fold]
    assert chroma.deleted_context == [fold]
    assert result.data["chroma_cleaned"] is True


def test_merge_dry_run_changes_nothing(chroma):
    keep = _mk_node("Alex")
    fold = _mk_node("Aleks")
    _add(NodeSectionTag(node_id=fold, namespace="wiki", section_name="Family"))
    _add(KGNodeVerdict(node_id_a=fold, verdict_type="verified",
                       memo="m", decided_by="test"))

    result = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="preview", dry_run=True)
    assert result.data.get("dry_run") is True
    assert _node_exists(fold)
    assert _count(NodeSectionTag, node_id=fold) == 1
    session = get_session()
    try:
        v = session.query(KGNodeVerdict).filter(KGNodeVerdict.node_id_a == fold).one()
    finally:
        session.close()
    assert v.superseded_at is None
    assert _merge_revision_rows() == []
    assert chroma.deleted == []


# ── P1.5(3): the shared helper does the same cleanup ──────────────────────


def test_merge_nodes_in_session_cleans_tags_verdicts_and_chroma(chroma):
    winner = _mk_node("Winner")
    loser = _mk_node("Loser")
    other = _mk_node("Bystander")
    _add(NodeSectionTag(node_id=loser, namespace="wiki", section_name="Family"))
    _add(KGNodeVerdict(node_id_a=loser, node_id_b=other, verdict_type="distinct",
                       memo="keep apart", decided_by="test"))
    _add(KGNodeEvidence(node_id=loser, derived_sentence="ev"))

    session = get_session()
    try:
        w = session.query(Node).filter(Node.id == winner).one()
        l = session.query(Node).filter(Node.id == loser).one()
        log_id = merge_nodes_in_session(
            session, loser_node=l, winner_node=w, merge_actor="test",
        )
        session.commit()
    finally:
        session.close()

    assert not _node_exists(loser)
    assert chroma.deleted == [loser]
    assert chroma.deleted_context == [loser]
    assert _count(NodeSectionTag, node_id=loser) == 0
    assert _count(NodeSectionTag, node_id=winner, namespace="wiki", section_name="Family") == 1
    assert _count(KGNodeEvidence, node_id=loser) == 0
    assert _count(KGNodeEvidence, node_id=winner) == 1

    session = get_session()
    try:
        v = session.query(KGNodeVerdict).filter(KGNodeVerdict.node_id_a == loser).one()
        log = session.query(KGMergeLog).filter(KGMergeLog.id == log_id).one()
    finally:
        session.close()
    assert v.superseded_at is not None
    assert winner in v.superseded_reason
    assert len(log.loser_snapshot_json["section_tags"]) == 1
    assert any(r["table"] == "kg_node_section_tag" for r in log.rebinds_json)
