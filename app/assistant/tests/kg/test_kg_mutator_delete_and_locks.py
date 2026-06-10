"""P0.1 + P0.3 acceptance tests (audit 2026-06-09).

P0.1: the kg_delete_node / kg_delete_edge / kg_create_edge tool names are
backed by the audited KGMutatorTool — every commit writes kg_revision_log,
requires a reason, supports dry_run; delete_node removes ALL edges
explicitly (the Edge→Node ON DELETE CASCADE was dropped 2026-05-10) and
clears the node's Chroma embeddings.

P0.3: every destructive handler refuses rows where ``locked_by_user_at``
is set, as a refusal ToolResult (not an exception).

Uses the kg conftest (isolated test DB, fresh Node/Edge tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.utils.pydantic_classes import ToolMessage
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_revision_log():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
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


def _mk_node(label: str, node_type: str = "Entity", locked: bool = False) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(
            id=nid, label=label, node_type=node_type,
            locked_by_user_at=NOW if locked else None,
        ))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(source_id: str, target_id: str, rel: str = "related_to", locked: bool = False) -> str:
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(
            id=eid, source_id=source_id, target_id=target_id, relationship_type=rel,
            locked_by_user_at=NOW if locked else None,
        ))
        session.commit()
    finally:
        session.close()
    return eid


def _node_exists(nid: str) -> bool:
    session = get_session()
    try:
        return session.query(Node).filter(Node.id == nid).count() == 1
    finally:
        session.close()


def _edges_touching(nid: str) -> int:
    session = get_session()
    try:
        return (
            session.query(Edge)
            .filter((Edge.source_id == nid) | (Edge.target_id == nid))
            .count()
        )
    finally:
        session.close()


def _revision_rows(op: str) -> list:
    session = get_session()
    try:
        return session.query(KGRevisionLog).filter(KGRevisionLog.op == op).all()
    finally:
        session.close()


def _is_refusal(result, *needles: str) -> bool:
    content = (result.content or "").lower()
    return all(n.lower() in content for n in needles)


# ── kg_delete_node (P0.1 acceptance) ─────────────────────────────────────


def test_delete_node_removes_edges_logs_and_cleans_chroma(chroma):
    a = _mk_node("Alpha")
    b = _mk_node("Beta")
    c = _mk_node("Gamma")
    _mk_edge(a, b, "knows")
    _mk_edge(c, a, "mentions")

    result = _call("kg_delete_node", node_id=a, reason="test deletion of Alpha")

    assert result.data.get("ok") is True, result.content
    assert not _node_exists(a)
    assert _node_exists(b) and _node_exists(c)
    assert _edges_touching(a) == 0  # no orphaned edge rows
    assert result.data.get("edges_deleted") == 2

    rows = _revision_rows("delete_node")
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == "test deletion of Alpha"
    assert row.before_json["node"]["label"] == "Alpha"
    assert len(row.before_json["edges"]) == 2

    assert chroma.deleted == [a]
    assert chroma.deleted_context == [a]
    assert result.data.get("chroma_cleaned") is True


def test_delete_node_dry_run_changes_nothing(chroma):
    a = _mk_node("Alpha")
    b = _mk_node("Beta")
    _mk_edge(a, b)

    result = _call("kg_delete_node", node_id=a, reason="preview", dry_run=True)

    assert result.data.get("dry_run") is True
    assert _node_exists(a)
    assert _edges_touching(a) == 1
    assert _revision_rows("delete_node") == []
    assert chroma.deleted == []


def test_delete_node_missing_node_refuses():
    result = _call("kg_delete_node", node_id=str(uuid.uuid4()), reason="x")
    assert result.result_type == "error"
    assert "not found" in (result.content or "")


def test_delete_node_requires_reason():
    a = _mk_node("Alpha")
    result = _call("kg_delete_node", node_id=a)
    assert result.result_type == "error"
    assert "reason" in (result.content or "")
    assert _node_exists(a)


def test_delete_node_refuses_locked_node(chroma):
    a = _mk_node("Axiom", locked=True)
    result = _call("kg_delete_node", node_id=a, reason="should refuse")
    assert _is_refusal(result, "user-locked", "kg_delete_node")
    assert _node_exists(a)
    assert _revision_rows("delete_node") == []
    assert chroma.deleted == []


def test_delete_node_refuses_when_attached_edge_locked(chroma):
    a = _mk_node("Alpha")
    b = _mk_node("Beta")
    _mk_edge(a, b, locked=True)
    result = _call("kg_delete_node", node_id=a, reason="should refuse")
    assert _is_refusal(result, "user-locked")
    assert _node_exists(a)
    assert _edges_touching(a) == 1


# ── kg_delete_edge / kg_create_edge through the audited path ─────────────


def test_delete_edge_writes_revision_log():
    a = _mk_node("Alpha")
    b = _mk_node("Beta")
    eid = _mk_edge(a, b, "knows")

    result = _call("kg_delete_edge", edge_id=eid, reason="duplicate fact")

    assert result.data.get("ok") is True, result.content
    assert _edges_touching(a) == 0
    rows = _revision_rows("delete_edge")
    assert len(rows) == 1
    assert rows[0].reason == "duplicate fact"


def test_delete_edge_refuses_locked_edge_and_locked_endpoint():
    a = _mk_node("Alpha")
    b = _mk_node("Beta")
    locked_edge = _mk_edge(a, b, "knows", locked=True)
    refusal = _call("kg_delete_edge", edge_id=locked_edge, reason="x")
    assert _is_refusal(refusal, "user-locked", "kg_delete_edge")
    assert _edges_touching(a) == 1

    c = _mk_node("LockedOwner", locked=True)
    d = _mk_node("Delta")
    plain_edge = _mk_edge(c, d, "owns")
    refusal2 = _call("kg_delete_edge", edge_id=plain_edge, reason="x")
    assert _is_refusal(refusal2, "user-locked")
    assert _edges_touching(c) == 1


def test_create_edge_writes_revision_log_and_validates():
    a = _mk_node("Alpha")
    b = _mk_node("Beta")

    result = _call(
        "kg_create_edge",
        source_id=a, target_id=b, relationship_type="works_for",
        reason="stated in chat", sentence="Alpha works for Beta",
    )
    assert result.data.get("ok") is True, result.content
    assert _edges_touching(a) == 1
    rows = _revision_rows("create_edge")
    assert len(rows) == 1

    # Exact duplicate refused.
    dup = _call(
        "kg_create_edge",
        source_id=a, target_id=b, relationship_type="works_for", reason="again",
    )
    assert dup.result_type == "error"
    assert "already exists" in (dup.content or "")

    # Missing endpoint refused.
    missing = _call(
        "kg_create_edge",
        source_id=a, target_id=str(uuid.uuid4()), relationship_type="knows", reason="x",
    )
    assert missing.result_type == "error"
    assert "not found" in (missing.content or "")


# ── P0.3: locked refusals on the remaining destructive handlers ──────────


def test_merge_refuses_locked_keep_or_fold():
    keep = _mk_node("Keep", locked=True)
    fold = _mk_node("Fold")
    r1 = _call("kg_merge_nodes", keep_id=keep, fold_id=fold, reason="x")
    assert _is_refusal(r1, "user-locked", "kg_merge_nodes")

    keep2 = _mk_node("Keep2")
    fold2 = _mk_node("Fold2", locked=True)
    r2 = _call("kg_merge_nodes", keep_id=keep2, fold_id=fold2, reason="x")
    assert _is_refusal(r2, "user-locked")
    assert _node_exists(fold2)


def test_rename_refuses_locked_node():
    a = _mk_node("Axiom", locked=True)
    r = _call("kg_rename_label", node_id=a, new_label="Renamed", reason="x")
    assert _is_refusal(r, "user-locked", "kg_rename_label")
    session = get_session()
    try:
        assert session.query(Node).filter(Node.id == a).one().label == "Axiom"
    finally:
        session.close()


def test_update_field_refuses_locked_node():
    a = _mk_node("Axiom", locked=True)
    r = _call("kg_update_node_field", node_id=a, field="description", value="new", reason="x")
    assert _is_refusal(r, "user-locked", "kg_update_node_field")


def test_close_state_refuses_locked_node():
    a = _mk_node("Living in Tampere", node_type="State", locked=True)
    r = _call("kg_close_state", node_id=a, end_date="2026-06-01", reason="x")
    assert _is_refusal(r, "user-locked", "kg_close_state")


# ── wrapper repoint regression guard ─────────────────────────────────────


def test_tool_wrappers_point_at_audited_mutator():
    from app.assistant.lib.tools.kg_create_edge.kg_create_edge import get_tool_class as create_edge_cls
    from app.assistant.lib.tools.kg_delete_edge.kg_delete_edge import get_tool_class as delete_edge_cls
    from app.assistant.lib.tools.kg_delete_node.kg_delete_node import get_tool_class as delete_node_cls

    assert delete_node_cls() is KGMutatorTool
    assert delete_edge_cls() is KGMutatorTool
    assert create_edge_cls() is KGMutatorTool
