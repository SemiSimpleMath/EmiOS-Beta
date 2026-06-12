"""P2.3 + P2.6 acceptance tests (audit 2026-06-09).

P2.3 — entity-like matching:
- the exact-label gate is the proposal's OWN node_type (a Goal no longer
  silently binds to a same-label Concept; cross-type allowlist is
  deliberately empty)
- same-label binds to well-connected person nodes require node_merger
  confirmation (the two-Alexes guard)
- unresolved entity-likes get Chroma semantic candidates (cosine >= 0.88,
  label + context collections, same-type only) for node_merger confirm

P2.6 — mint/match race: a "create" decision re-checks for a concurrently
minted entity inside the write transaction and converts to match — except
when the hit is a confirm-tier person (a silent bind is exactly what the
guard exists to prevent).

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.claim_proposals import (
    ClaimProposal,
    ClaimProposalEvidence,
    ClaimProposalNode,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.proposal_promoter import (
    _PreparedNode,
    _PromoterPlan,
    _evaluate_and_apply,
    _needs_person_confirm,
    _resolve_entity_like,
    _semantic_entity_candidates,
)
from app.models.base import get_session

T = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _mk_node(label: str, node_type: str = "Entity", **fields) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edges(nid: str, n: int) -> None:
    session = get_session()
    try:
        for i in range(n):
            other = Node(id=str(uuid.uuid4()), label=f"N{i}", node_type="Entity")
            session.add(other)
            session.flush()
            session.add(Edge(id=str(uuid.uuid4()), source_id=nid,
                             target_id=other.id, relationship_type="relates_to"))
        session.commit()
    finally:
        session.close()


# ── P2.3: type gate ──────────────────────────────────────────────────────


def test_entity_resolution_gated_to_own_type():
    _mk_node("Fitness", node_type="Concept")
    session = get_session()
    try:
        # A Goal proposal no longer binds to the same-label Concept…
        assert _resolve_entity_like(session, "Fitness", "Goal") is None
        # …while the same-type lookup still works.
        hit, tier = _resolve_entity_like(session, "Fitness", "Concept")
        assert hit.node_type == "Concept" and tier == "label"
    finally:
        session.close()


# ── alias tier is not closed-form (the Jouko's-house misbind, 2026-06-12) ─


def test_alias_hit_returns_alias_tier():
    # "House" became an alias of a specific home via P2.2(c) accretion; an
    # alias hit must be distinguishable from an exact-label hit so the
    # caller can route it through node_merger instead of silently binding.
    _mk_node("Tom's House", aliases=["House"])
    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "House", "Entity")
        assert hit.label == "Tom's House" and tier == "alias"
    finally:
        session.close()


# ── P2.3: two-Alexes person-confirm trigger ─────────────────────────────


def test_person_confirm_requires_category_and_edges():
    sparse_person = _mk_node("Alex", category="person")
    busy_person = _mk_node("Sam", category="person")
    _mk_edges(busy_person, 5)
    busy_place = _mk_node("Springfield", category="place")
    _mk_edges(busy_place, 5)

    session = get_session()
    try:
        assert _needs_person_confirm(session, session.get(Node, sparse_person)) is False
        assert _needs_person_confirm(session, session.get(Node, busy_person)) is True
        assert _needs_person_confirm(session, session.get(Node, busy_place)) is False
    finally:
        session.close()


# ── P2.3: semantic candidate tier ────────────────────────────────────────


class _FakeCollection:
    def __init__(self, rows):  # rows: list of (id, embedding)
        self._rows = rows

    def query(self, *, query_embeddings, n_results, include):
        return {
            "ids": [[cid for cid, _ in self._rows[:n_results]]],
            "embeddings": [[emb for _, emb in self._rows[:n_results]]],
        }


def test_semantic_tier_returns_same_type_candidates_above_threshold(monkeypatch):
    near = _mk_node("The user's mother", node_type="Entity")
    wrong_type = _mk_node("Motherhood", node_type="Concept")
    far = _mk_node("Bicycle", node_type="Entity")

    query_vec = [1.0, 0.0, 0.0]
    near_vec = [0.95, 0.05, 0.0]      # cosine ≈ 0.999 → keep
    far_vec = [0.0, 1.0, 0.0]         # cosine 0 → drop

    import app.assistant.embeddings.embedder as embedder_mod
    import app.assistant.kg.chroma.chroma_embedding_manager as cem
    monkeypatch.setattr(embedder_mod, "embed_text", lambda text: query_vec)
    fake_cm = SimpleNamespace(
        node_collection=_FakeCollection([(near, near_vec), (far, far_vec)]),
        node_context_collection=_FakeCollection([(wrong_type, near_vec)]),
    )
    monkeypatch.setattr(cem, "get_chroma_manager", lambda: fake_cm)

    session = get_session()
    try:
        cands = _semantic_entity_candidates(session, "The user's mom", "Entity")
    finally:
        session.close()

    ids = {c["node_id"] for c in cands}
    assert near in ids           # similar + same type → candidate
    assert far not in ids        # dissimilar → dropped
    assert wrong_type not in ids # similar but Concept ≠ Entity → dropped


def test_semantic_tier_degrades_to_empty_without_chroma(monkeypatch):
    import app.assistant.kg.chroma.chroma_embedding_manager as cem

    def boom():
        raise RuntimeError("chroma down")
    monkeypatch.setattr(cem, "get_chroma_manager", boom)

    session = get_session()
    try:
        assert _semantic_entity_candidates(session, "Anything", "Entity") == []
    finally:
        session.close()


# ── P2.6: mint/match race guard ──────────────────────────────────────────


def _proposal_with_entity(label: str):
    session = get_session()
    try:
        p = ClaimProposal(id=str(uuid.uuid4()), status="pending",
                          claim_type="durable",
                          first_observed_at=T, last_observed_at=T)
        session.add(p)
        session.flush()
        session.add(ClaimProposalEvidence(
            id=str(uuid.uuid4()), proposal_id=p.id, source_type="chat",
            raw_text=f"mention of {label}", observed_at=T,
            window_id=str(uuid.uuid4()),
        ))
        pn = ClaimProposalNode(
            id=str(uuid.uuid4()), proposal_id=p.id, label=label,
            node_type="Entity", sentence=f"{label} exists.",
        )
        session.add(pn)
        session.commit()
        return p.id, pn.id
    finally:
        session.close()


def _apply_create_plan(proposal_id: str, pn_id: str, label: str):
    plan = _PromoterPlan(proposal_id=proposal_id, nodes={
        pn_id: _PreparedNode(pn_id=pn_id, pn_label=label,
                             pn_node_type="Entity", decision="create"),
    })
    session = get_session()
    try:
        p = session.query(ClaimProposal).filter_by(id=proposal_id).one()
        dec = _evaluate_and_apply(session, p, plan, commit=True)
        session.commit()
        return session, dec
    except Exception:
        session.close()
        raise


def test_race_guard_converts_concurrent_mint_to_match():
    # The plan decided "create"; meanwhile a concurrent run minted the node.
    existing = _mk_node("Race Winner", node_type="Entity")
    pid, pn_id = _proposal_with_entity("Race Winner")

    session, dec = _apply_create_plan(pid, pn_id, "Race Winner")
    try:
        outcome = dec.node_outcomes[0]
        assert outcome.action == "matched_existing"
        assert outcome.resolved_node_id == existing
        n = session.query(Node).filter(Node.label == "Race Winner").count()
        assert n == 1                          # no double-mint
    finally:
        session.close()


def test_race_guard_does_not_silently_bind_alias_tier_hit():
    # The concurrent "hit" is only an alias match ("House" ∈ aliases of a
    # specific home). A silent alias bind is the exact failure the confirm
    # tier exists to prevent — the race guard must create, not convert.
    home = _mk_node("Tom's House", aliases=["House"])
    pid, pn_id = _proposal_with_entity("House")

    session, dec = _apply_create_plan(pid, pn_id, "House")
    try:
        outcome = dec.node_outcomes[0]
        assert outcome.action == "created_new"
        assert outcome.resolved_node_id != home
    finally:
        session.close()


def test_race_guard_does_not_silently_bind_confirm_tier_person():
    # The concurrent hit is a well-connected person — exactly the case the
    # two-Alexes guard protects. The race guard must NOT convert; a second
    # node is the safe outcome (the dup scan + merger can heal it later).
    busy = _mk_node("Alexis", node_type="Entity", category="person")
    _mk_edges(busy, 5)
    pid, pn_id = _proposal_with_entity("Alexis")

    session, dec = _apply_create_plan(pid, pn_id, "Alexis")
    try:
        outcome = dec.node_outcomes[0]
        assert outcome.action == "created_new"
        n = (
            session.query(Node)
            .filter(Node.label == "Alexis", Node.node_type == "Entity")
            .count()
        )
        assert n == 2
        # The label now has two referents — the attach-here rework mints a
        # Disambiguation marker so FUTURE "Alexis" mentions park there
        # instead of pagerank-guessing between the twins.
        dis = (
            session.query(Node)
            .filter(Node.label == "Alexis", Node.node_type == "Disambiguation")
            .count()
        )
        assert dis == 1
    finally:
        session.close()
