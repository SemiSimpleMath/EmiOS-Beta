"""kg_set_user_lock — the user-lock (axiom layer) mutator op.

Lock stamps locked_by_user_at + confidence_tier='axiom'; unlock clears the
stamp and returns the tier to 'provisional'. This is the ONLY mutator path
allowed to touch a locked row (unlock = the user revoking the guarantee).
Audited like every op: reason required, dry_run preview, revision log row.

Uses the kg conftest (isolated test DB, fresh Node/Edge tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.lib.core_tools.kg_mutator.kg_mutator_tool import KGMutatorTool
from app.assistant.utils.pydantic_classes import ToolMessage
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_revision_log():
    session = get_session()
    try:
        session.query(KGRevisionLog).delete()
        session.commit()
    finally:
        session.close()
    yield


def _call(tool_name: str, **arguments):
    msg = ToolMessage(tool_name=tool_name, tool_data={"tool_name": tool_name, "arguments": arguments})
    return KGMutatorTool().execute(msg)


def _mk_node(label: str, *, locked: bool = False, tier: str = "provisional") -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(
            id=nid, label=label, node_type="Entity",
            locked_by_user_at=NOW if locked else None,
            confidence_tier=tier,
        ))
        session.commit()
    finally:
        session.close()
    return nid


def _fetch(nid: str) -> Node:
    session = get_session()
    try:
        return session.query(Node).filter(Node.id == nid).one()
    finally:
        session.close()


def test_lock_sets_stamp_and_axiom_tier():
    nid = _mk_node("Marriage")
    res = _call("kg_set_user_lock", node_id=nid, locked=True, reason="user pinned it")
    assert res.data.get("ok") is True

    node = _fetch(nid)
    assert node.locked_by_user_at is not None
    assert node.confidence_tier == "axiom"

    session = get_session()
    try:
        row = session.query(KGRevisionLog).filter(KGRevisionLog.op == "set_user_lock").one()
        assert row.reason == "user pinned it"
        assert row.before_json["confidence_tier"] == "provisional"
        assert row.after_json["confidence_tier"] == "axiom"
    finally:
        session.close()


def test_unlock_clears_stamp_and_returns_provisional():
    nid = _mk_node("Marriage", locked=True, tier="axiom")
    res = _call("kg_set_user_lock", node_id=nid, locked=False, reason="user revoked the pin")
    assert res.data.get("ok") is True

    node = _fetch(nid)
    assert node.locked_by_user_at is None
    assert node.confidence_tier == "provisional"


def test_noop_transition_refused():
    nid = _mk_node("Marriage", locked=True, tier="axiom")
    res = _call("kg_set_user_lock", node_id=nid, locked=True, reason="again")
    assert res.data.get("ok") is not True
    assert "no-op" in str(res.content)

    nid2 = _mk_node("Unlocked Thing")
    res2 = _call("kg_set_user_lock", node_id=nid2, locked=False, reason="already unlocked")
    assert res2.data.get("ok") is not True


def test_dry_run_changes_nothing():
    nid = _mk_node("Marriage")
    res = _call("kg_set_user_lock", node_id=nid, locked=True, reason="preview", dry_run=True)
    assert res.data.get("ok") is True
    assert res.data.get("dry_run") is True

    node = _fetch(nid)
    assert node.locked_by_user_at is None
    assert node.confidence_tier == "provisional"

    session = get_session()
    try:
        assert session.query(KGRevisionLog).count() == 0
    finally:
        session.close()


def test_reason_required_and_missing_node_refused():
    nid = _mk_node("Marriage")
    res = _call("kg_set_user_lock", node_id=nid, locked=True, reason="")
    assert res.data.get("ok") is not True

    res2 = _call("kg_set_user_lock", node_id=str(uuid.uuid4()), locked=True, reason="ghost")
    assert res2.data.get("ok") is not True
    assert "not found" in str(res2.content)


def test_locked_node_still_refuses_other_ops_but_unlock_opens_it():
    nid = _mk_node("Marriage")
    _call("kg_set_user_lock", node_id=nid, locked=True, reason="pin")

    res = _call("kg_update_node_field", node_id=nid, field="description",
                value="should be refused", list_op="", reason="attempt")
    assert res.data.get("ok") is not True
    assert "user-locked" in str(res.content)

    _call("kg_set_user_lock", node_id=nid, locked=False, reason="unpin")
    res2 = _call("kg_update_node_field", node_id=nid, field="description",
                 value="now it works", list_op="", reason="after unlock")
    assert res2.data.get("ok") is True
