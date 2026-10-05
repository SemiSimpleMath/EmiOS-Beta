"""The promoter's model calls fail loud (2026-10-05).

On 2026-06-27 a network outage made node_merger fail while confirming the owner's label; the failure read as "no
match", so the promoter minted a second owner Entity and then a Disambiguation marker at the label, which captured
every later mention of the owner (381 edges by 2026-10-05). Pins: a failed or malformed merger answer raises (the
proposal stays pending and is retried) while a real decline still returns None; the TTL estimator and the
canonicalizer raise instead of writing a degraded node; a Property label collision mints no marker.

Uses the kg conftest (isolated test DB, fresh tables per test). Invented data only.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.assistant.kg import proposal_promoter as P
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.models.base import get_session


class _Agent:
    def __init__(self, data=None, exc=None):
        self.data, self.exc = data, exc

    def action_handler(self, msg):
        if self.exc:
            raise self.exc
        return SimpleNamespace(data=self.data)


@pytest.fixture
def agent(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    holder = {}
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: holder.get("agent"))
    monkeypatch.setattr("app.assistant.kg_maintenance.verdict_store.load_distinct_verdicts_among", lambda ids: [])
    return holder


CANDS = [{"node_id": "n1", "label": "Sam"}]


def test_a_failed_merger_call_raises_instead_of_creating(agent):
    agent["agent"] = _Agent(exc=RuntimeError("Connection error."))
    with pytest.raises(RuntimeError, match="Connection error"):
        P._call_node_merger_for_state_match({"label": "Sam"}, CANDS)


def test_a_missing_merger_raises(agent):
    with pytest.raises(RuntimeError, match="could not create node_merger"):
        P._call_node_merger_for_state_match({"label": "Sam"}, CANDS)


def test_a_merge_into_a_non_candidate_raises_and_a_decline_returns_none(agent):
    agent["agent"] = _Agent(data={"merge_nodes": True, "merged_node_id": "elsewhere"})
    with pytest.raises(RuntimeError, match="not a candidate"):
        P._call_node_merger_for_state_match({"label": "Sam"}, CANDS)
    agent["agent"] = _Agent(data={"merge_nodes": False})
    assert P._call_node_merger_for_state_match({"label": "Sam"}, CANDS) is None
    agent["agent"] = _Agent(data={"merge_nodes": True, "merged_node_id": "n1"})
    assert P._call_node_merger_for_state_match({"label": "Sam"}, CANDS) == "n1"


def test_ttl_and_canonicalizer_failures_raise(agent):
    node = SimpleNamespace(label="Call", node_type="State", description_draft="", valid_from=None, valid_to=None,
                           sentence="Sam calls on Mondays.", attributes_json={})
    agent["agent"] = _Agent(exc=RuntimeError("Connection error."))
    with pytest.raises(RuntimeError):
        P._estimate_state_ttl(node)
    with pytest.raises(RuntimeError):
        P._canonicalize_sentence(node)
    agent["agent"] = _Agent(data={})
    with pytest.raises(RuntimeError, match="no duration class"):
        P._estimate_state_ttl(node)
    with pytest.raises(RuntimeError, match="no sentence"):
        P._canonicalize_sentence(node)


def test_a_property_label_collision_mints_no_marker():
    from app.assistant.kg.disambiguation import find_disambiguation
    s = get_session()
    try:
        for _ in range(2):
            s.add(Node(id=str(uuid.uuid4()), label="Date of Birth", node_type="Property"))
        s.flush()
        new = s.query(Node).filter(Node.label == "Date of Birth").first()
        P._mint_disambiguation_on_label_collision(s, new)
        assert find_disambiguation(s, "Date of Birth") is None
        s.rollback()
    finally:
        s.close()


def test_an_exact_label_bind_to_a_locked_person_needs_no_confirmation():
    """Owner, 2026-10-05: "there can be no other [owner] nodes" — a locked person is the one referent of its label, so
    a mention binds without a node_merger call (a failed call minted the owner's twin on 2026-06-27)."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge
    from datetime import datetime, timezone
    s = get_session()
    try:
        locked = Node(id=str(uuid.uuid4()), label="Sam", node_type="Entity", category="person",
                      locked_by_user_at=datetime(2026, 5, 10, tzinfo=timezone.utc))
        unlocked = Node(id=str(uuid.uuid4()), label="Pat", node_type="Entity", category="person")
        s.add_all([locked, unlocked])
        for n in (locked, unlocked):
            for _ in range(P._PERSON_CONFIRM_MIN_EDGES):
                other = Node(id=str(uuid.uuid4()), label="x", node_type="State")
                s.add(other)
                s.add(Edge(id=str(uuid.uuid4()), source_id=n.id, target_id=other.id, relationship_type="has_state"))
        s.flush()
        assert P._needs_person_confirm(s, locked) is False
        assert P._needs_person_confirm(s, unlocked) is True, "an unlocked well-connected person still needs it"
        s.rollback()
    finally:
        s.close()

