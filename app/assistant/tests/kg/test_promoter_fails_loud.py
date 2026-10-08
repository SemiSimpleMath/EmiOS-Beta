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
    agent["agent"] = _Agent(data={"merge_nodes": True, "merged_candidate": "N7"})
    with pytest.raises(RuntimeError, match="not a candidate"):
        P._call_node_merger_for_state_match({"label": "Sam"}, CANDS)
    agent["agent"] = _Agent(data={"merge_nodes": False})
    assert P._call_node_merger_for_state_match({"label": "Sam"}, CANDS) is None
    agent["agent"] = _Agent(data={"merge_nodes": True, "merged_candidate": "N1"})
    assert P._call_node_merger_for_state_match({"label": "Sam"}, CANDS) == "n1"


def test_the_merger_sees_labels_never_ids_and_its_label_maps_back(agent, monkeypatch):
    """2026-10-08: shown the candidates' UUIDs and their source window ids, the merger returned the owner's son's node
    id spliced with his source window's id, four times. It now sees N1, N2... and no id; code maps the label back."""
    a = "d62686dd-3881-4da9-8dff-83b28ba80c7d"
    b = "11111111-2222-4333-8444-555555555555"
    cands = [{"node_id": a, "label": "Sam", "description": "x" * 900},
             {"node_id": b, "label": "Sam", "description": "a cousin"}]
    monkeypatch.setattr("app.assistant.kg_maintenance.verdict_store.load_distinct_verdicts_among",
                        lambda ids: [(a, b, "different people", "investigator")])
    seen = {}

    class Recording(_Agent):
        def action_handler(self, msg):
            seen["input"] = msg.agent_input
            return super().action_handler(msg)
    agent["agent"] = Recording(data={"merge_nodes": True, "merged_candidate": "N2"})
    assert P._call_node_merger_for_state_match({"label": "Sam"}, cands) == b
    shown = seen["input"]["existing_node_candidates"]
    assert a not in shown and b not in shown and "d62686dd" not in shown, "no id or id prefix reaches the model"
    assert '"candidate": "N1"' in shown and '"candidate": "N2"' in shown
    assert "DISTINCT from candidate N2" in shown and "DISTINCT from candidate N1" in shown
    assert "x" * 900 in shown, "the description reaches the model whole"
    assert cands[0]["node_id"] == a and "prior_distinct_verdicts" not in cands[0], "the caller's list is untouched"


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


def test_a_failed_context_embedding_at_mint_raises(monkeypatch):
    """The new node's context vector failing to store stops the proposal (it used to warn, leaving the node invisible
    to the next proposals' semantic tier). The conftest stubs this helper; the original is exercised here."""
    monkeypatch.undo()  # drop the conftest stub: exercise the real helper
    import app.assistant.kg.chroma.chroma_embedding_manager as cem

    def boom():
        raise RuntimeError("chroma down")
    monkeypatch.setattr(cem, "get_chroma_manager", boom)
    with pytest.raises(RuntimeError, match="chroma down"):
        P._embed_and_store_node("n1", "Sam", "Sam has a call on Mondays.")


def test_the_promoter_run_stops_at_the_first_failed_proposal_and_counts_it(monkeypatch):
    """Owner, 2026-10-05: any failed call in the KG stops processing. The failed proposal stays pending, the rest are
    not touched this run, and the failure is counted in the ledger (three failed runs set it aside)."""
    from datetime import datetime, timezone
    from app.assistant.database.claim_proposals import ClaimProposal
    from app.assistant.kg import failure_ledger
    monkeypatch.setattr("app.assistant.utils.subsystem_flags.is_subsystem_enabled", lambda name: True)
    t0 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    s = get_session()
    try:
        failure_ledger._ensure(s)
        s.query(failure_ledger.KGItemFailure).delete()
        s.query(ClaimProposal).delete()
        ids = []
        for i in range(2):
            pid = str(uuid.uuid4())
            ids.append(pid)
            s.add(ClaimProposal(id=pid, status="pending", claim_type="durable",
                                created_at=datetime(2026, 10, 5, i, tzinfo=timezone.utc),
                                first_observed_at=t0, last_observed_at=t0))
        s.commit()
    finally:
        s.close()
    prepared = []

    def plan(pid):
        prepared.append(pid)
        raise RuntimeError("Connection error.")
    monkeypatch.setattr(P, "_prepare_proposal_plan", plan)
    with pytest.raises(RuntimeError, match="Connection error"):
        P.run_promoter(limit=10, commit=True)
    assert prepared == [ids[0]], "the run stops at the first failure"
    s = get_session()
    try:
        assert {p.status for p in s.query(ClaimProposal).all()} == {"pending"}
        row = s.get(failure_ledger.KGItemFailure, ("kg_proposal", ids[0]))
        assert row.failures == 1 and "Connection error" in row.last_error
    finally:
        s.close()

