"""Durable mention map (identity phase 5, 2026-06-12).

Confirmed binds are recorded once and consulted closed-form; everything
alias accretion got wrong is designed out: mint only on LLM confirmation,
only for graph-wide-unambiguous forms, self-revoking on ambiguity.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid

import pytest

from app.assistant.database.kg_mention_map import KGMentionMap
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg.mention_map import lookup_mention, mint_mention
from app.assistant.kg.proposal_promoter import (
    _evaluate_and_apply,
    _PreparedNode,
    _PromoterPlan,
    _resolve_entity_like,
)
from app.models.base import get_session


@pytest.fixture(autouse=True)
def _clean_mention_map():
    # The kg conftest wipes the node tables per test but not this one —
    # stale entries from a previous run would point at wiped nodes.
    session = get_session()
    try:
        session.query(KGMentionMap).delete()
        session.commit()
    finally:
        session.close()
    yield


def _mk_node(label, node_type="Entity", **fields):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=node_type, **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _entries(norm=None):
    session = get_session()
    try:
        q = session.query(KGMentionMap)
        if norm:
            q = q.filter(KGMentionMap.mention_norm == norm)
        return q.all()
    finally:
        session.close()


# ── mint rules ───────────────────────────────────────────────────────────


def test_mint_and_closed_form_lookup_roundtrip():
    house = _mk_node("Tom and Sue's House")
    session = get_session()
    try:
        minted, why = mint_mention(
            session, label="the house up north", node_type="Entity",
            node_id=house, minted_by="test")
        assert minted, why
        hit = lookup_mention(session, "The House Up  North", "Entity")
        assert hit is not None and hit.id == house
        session.commit()
    finally:
        session.close()
    entry = _entries("the house up north")[0]
    assert entry.use_count == 1 and entry.revoked_at is None


def test_mint_refuses_own_label_and_contested_forms():
    a = _mk_node("Springfield")
    b = _mk_node("Springfield Gardens", aliases=["the gardens"])
    session = get_session()
    try:
        minted, why = mint_mention(session, label="Springfield",
                                   node_type="Entity", node_id=a, minted_by="t")
        assert not minted and "own label" in why

        # Contested: another same-type node claims the form as an alias.
        minted, why = mint_mention(session, label="the gardens",
                                   node_type="Entity", node_id=a, minted_by="t")
        assert not minted and "alias" in why
    finally:
        session.close()


def test_conflicting_confirmed_bind_revokes_existing_entry():
    a, b = _mk_node("Thing A"), _mk_node("Thing B")
    session = get_session()
    try:
        assert mint_mention(session, label="the gadget", node_type="Entity",
                            node_id=a, minted_by="t")[0]
        minted, why = mint_mention(session, label="the gadget",
                                   node_type="Entity", node_id=b, minted_by="t")
        assert not minted and "ambiguous" in why
        session.commit()
    finally:
        session.close()
    entry = _entries("the gadget")[0]
    assert entry.revoked_at is not None  # the form proved ambiguous


def test_lookup_self_revokes_when_target_gone_or_contested():
    a = _mk_node("Original Thing")
    session = get_session()
    try:
        mint_mention(session, label="the doohickey", node_type="Entity",
                     node_id=a, minted_by="t")
        session.commit()
        # Target disappears (merge/delete).
        session.delete(session.get(Node, a))
        session.commit()
        assert lookup_mention(session, "the doohickey", "Entity") is None
        session.commit()
    finally:
        session.close()
    assert _entries("the doohickey")[0].revoked_at is not None


# ── resolution ladder integration ────────────────────────────────────────


def test_resolver_returns_mention_map_tier_after_alias_removal():
    target = _mk_node("Tom's Workshop")
    session = get_session()
    try:
        mint_mention(session, label="the workshop", node_type="Entity",
                     node_id=target, minted_by="t")
        session.commit()
        hit, tier = _resolve_entity_like(session, "the workshop", "Entity")
        assert hit.id == target and tier == "mention_map"
    finally:
        session.close()


def test_exact_label_precedes_mention_map():
    target = _mk_node("Old Referent")
    session = get_session()
    try:
        mint_mention(session, label="workshop two", node_type="Entity",
                     node_id=target, minted_by="t")
        session.commit()
    finally:
        session.close()
    # A node whose LABEL is the form arrives later: label tier wins (and
    # the map's own lookup would revoke on contest anyway).
    labeled = _mk_node("Workshop Two")
    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "workshop two", "Entity")
        assert hit.id == labeled and tier == "label"
    finally:
        session.close()


# ── promoter apply-phase minting ─────────────────────────────────────────


def test_confirmed_bind_mints_mention_entry():
    from app.assistant.database.claim_proposals import (
        ClaimProposal, ClaimProposalEvidence, ClaimProposalNode,
    )
    from datetime import datetime, timezone

    T = datetime(2026, 6, 1, tzinfo=timezone.utc)
    target = _mk_node("Dave Weisbart")

    session = get_session()
    try:
        p = ClaimProposal(id=str(uuid.uuid4()), status="pending",
                          claim_type="durable",
                          first_observed_at=T, last_observed_at=T)
        session.add(p)
        session.flush()
        session.add(ClaimProposalEvidence(
            id=str(uuid.uuid4()), proposal_id=p.id, source_type="chat",
            raw_text="mention of dave", observed_at=T,
            window_id=str(uuid.uuid4())))
        pn = ClaimProposalNode(
            id=str(uuid.uuid4()), proposal_id=p.id, label="Dave",
            node_type="Entity", sentence="Dave exists.")
        session.add(pn)
        session.commit()
        pid, pn_id = p.id, pn.id
    finally:
        session.close()

    plan = _PromoterPlan(proposal_id=pid, nodes={
        pn_id: _PreparedNode(pn_id=pn_id, pn_label="Dave",
                             pn_node_type="Entity", decision="match",
                             matched_node_id=target, from_confirm=True),
    })
    session = get_session()
    try:
        p = session.query(ClaimProposal).filter_by(id=pid).one()
        _evaluate_and_apply(session, p, plan, commit=True)
        session.commit()
    finally:
        session.close()

    entry = _entries("dave")[0]
    assert entry.node_id == target and entry.revoked_at is None
    # And the next resolution of "Dave" is closed-form.
    session = get_session()
    try:
        hit, tier = _resolve_entity_like(session, "Dave", "Entity")
        assert hit.id == target and tier == "mention_map"
    finally:
        session.close()
