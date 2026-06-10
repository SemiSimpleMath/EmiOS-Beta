"""Tests that the proposal_promoter writes kg_node_evidence + kg_edge_evidence
rows at all six commit sites.

Pre-rebuild (before commit 27530c59 on 2026-04-26), these writes lived in
``write_node_evidence`` / ``write_edge_evidence`` helpers in the legacy
merge_utils. The rebuild moved node creation into proposal_promoter but
forgot to migrate the evidence-write step. Result: every node promoted
between 2026-04-26 and 2026-05-04 has zero kg_node_evidence rows, breaking
the kg_node_viewer's provenance UI and starving node_merger of context.

The forward fix added _write_node_evidence + _write_edge_evidence helpers
and called them from the 6 commit sites in _evaluate_and_apply. These tests
guard against regression at each site:

  1. Entity-like create  (Entity / Concept / Goal / Property)
  2. Entity-like match
  3. Relationship-like create  (State / Event)
  4. Relationship-like match
  5. Edge create
  6. Edge match (was working pre-fix; covered for parity)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.assistant.database.claim_proposals import (
    ClaimProposal,
    ClaimProposalEdge,
    ClaimProposalEvidence,
    ClaimProposalNode,
)
from app.assistant.database.kg_chat_projection import KGEdgeEvidence, KGNodeEvidence
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.proposal_promoter import (
    _PreparedNode,
    _PromoterPlan,
    _evaluate_and_apply,
)
from app.models.base import get_session


def _seed_proposal(session, *, window_id: str, raw_text: str, observed_at: datetime):
    """Create a minimal claim_proposal + evidence row. Returns the proposal."""
    proposal = ClaimProposal(
        id=str(uuid.uuid4()),
        status="pending",
        claim_type="durable",
        representative_sentence=raw_text,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
    )
    session.add(proposal)
    session.flush()
    session.add(ClaimProposalEvidence(
        id=str(uuid.uuid4()),
        proposal_id=proposal.id,
        source_type="chat",
        room_id="master_room",
        speaker_name="Jukka",
        speaker_role="user",
        window_id=window_id,
        unified_log_id=str(uuid.uuid4()),
        raw_text=raw_text,
        observed_at=observed_at,
    ))
    session.flush()
    return proposal


def _seed_pnode(session, proposal_id: str, *, label: str, node_type: str, sentence: str):
    pn = ClaimProposalNode(
        id=str(uuid.uuid4()),
        proposal_id=proposal_id,
        extractor_temp_id=f"n_{label.lower()}",
        label=label,
        node_type=node_type,
        category=None,
        sentence=sentence,
    )
    session.add(pn)
    session.flush()
    return pn


def _seed_pedge(session, proposal_id: str, *, src_pn_id: str, tgt_pn_id: str,
                predicate: str, sentence: str):
    pe = ClaimProposalEdge(
        id=str(uuid.uuid4()),
        proposal_id=proposal_id,
        source_node_id=src_pn_id,
        target_node_id=tgt_pn_id,
        predicate=predicate,
        sentence=sentence,
    )
    session.add(pe)
    session.flush()
    return pe


# ============================================================================
# Entity-like create (Entity, Concept, Goal, Property)
# ============================================================================

class TestEntityLikeCreate:

    def test_create_writes_node_evidence(self):
        observed_at = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())
        raw_text = "Jukka mentioned a new colleague named Pat."

        session = get_session()
        try:
            proposal = _seed_proposal(
                session, window_id=window_id,
                raw_text=raw_text, observed_at=observed_at,
            )
            pn = _seed_pnode(
                session, proposal.id,
                label="Pat", node_type="Entity", sentence="Pat is a colleague.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_id = pn.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_id: _PreparedNode(
                    pn_id=pn_id,
                    pn_label="Pat",
                    pn_node_type="Entity",
                    decision="create",
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(session, p, plan, commit=True)
            session.commit()
            assert dec.final_status == "promoted"
            assert any(o.action == "created_new" for o in dec.node_outcomes)
        finally:
            session.close()

        # Verify exactly one kg_node_evidence row, with the right linkage.
        session = get_session()
        try:
            ev_rows = session.query(KGNodeEvidence).all()
            assert len(ev_rows) == 1
            ev = ev_rows[0]
            assert ev.merge_action == "created"
            assert ev.window_id == window_id
            assert ev.source_text == raw_text
            # Provenance is window-level only since the belief-v2 seam: the
            # legacy source_table/source_id pair always pointed at the
            # window's FIRST message regardless of which claim, so the
            # promoter now deliberately writes NULL (walk window_id →
            # kg_window_message → unified_log_2026 for context).
            assert ev.source_table is None
            assert ev.source_id is None
            # SQLite strips tzinfo on round-trip; compare on the value itself.
            assert ev.message_timestamp.replace(tzinfo=None) == observed_at.replace(tzinfo=None)
            # node_id should match the freshly-created Node.
            new_node = session.query(Node).filter(Node.label == "Pat").first()
            assert new_node is not None
            assert ev.node_id == new_node.id
        finally:
            session.close()


# ============================================================================
# Entity-like match
# ============================================================================

class TestEntityLikeMatch:

    def test_match_writes_node_evidence_with_confirmed_action(self):
        observed_at = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())

        # Pre-seed an existing entity to match against.
        session = get_session()
        try:
            existing = Node(
                label="Sam", node_type="Entity",
                category=None, attributes={}, source="seed",
            )
            session.add(existing)
            session.flush()
            existing_id = existing.id

            proposal = _seed_proposal(
                session, window_id=window_id,
                raw_text="Jukka talked about Sam again today.",
                observed_at=observed_at,
            )
            pn = _seed_pnode(
                session, proposal.id,
                label="Sam", node_type="Entity",
                sentence="Sam is being mentioned again.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_id = pn.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_id: _PreparedNode(
                    pn_id=pn_id,
                    pn_label="Sam",
                    pn_node_type="Entity",
                    decision="match",
                    matched_node_id=existing_id,
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(session, p, plan, commit=True)
            session.commit()
            assert dec.final_status == "promoted"
            assert any(o.action == "matched_existing" for o in dec.node_outcomes)
        finally:
            session.close()

        session = get_session()
        try:
            ev_rows = session.query(KGNodeEvidence).all()
            assert len(ev_rows) == 1
            ev = ev_rows[0]
            assert ev.merge_action == "confirmed"
            assert ev.node_id == existing_id
            assert ev.window_id == window_id
        finally:
            session.close()


# ============================================================================
# Relationship-like create (State / Event)
# ============================================================================

class TestRelationshipLikeCreate:

    def test_create_writes_node_evidence_with_created_action(self):
        observed_at = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())

        session = get_session()
        try:
            proposal = _seed_proposal(
                session, window_id=window_id,
                raw_text="Jukka said he started feeling tired today.",
                observed_at=observed_at,
            )
            pn = _seed_pnode(
                session, proposal.id,
                label="Tiredness", node_type="State",
                sentence="Jukka feels tired.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_id = pn.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_id: _PreparedNode(
                    pn_id=pn_id,
                    pn_label="Tiredness",
                    pn_node_type="State",
                    decision="create",
                    ttl=None,
                    canonical_sentence="Jukka feels tired.",
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(session, p, plan, commit=True)
            session.commit()
            assert dec.final_status == "promoted"
        finally:
            session.close()

        session = get_session()
        try:
            ev_rows = session.query(KGNodeEvidence).all()
            assert len(ev_rows) == 1
            assert ev_rows[0].merge_action == "created"
            assert ev_rows[0].window_id == window_id
        finally:
            session.close()


# ============================================================================
# Edge create + edge match
# ============================================================================

class TestEdgeEvidence:

    def test_edge_create_writes_evidence(self):
        observed_at = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())

        session = get_session()
        try:
            proposal = _seed_proposal(
                session, window_id=window_id,
                raw_text="Pat works at Acme Corp.",
                observed_at=observed_at,
            )
            pn_pat = _seed_pnode(session, proposal.id,
                label="Pat", node_type="Entity",
                sentence="Pat works.")
            pn_acme = _seed_pnode(session, proposal.id,
                label="Acme Corp", node_type="Entity",
                sentence="Acme Corp is a company.")
            pe = _seed_pedge(
                session, proposal.id,
                src_pn_id=pn_pat.id, tgt_pn_id=pn_acme.id,
                predicate="works_at", sentence="Pat works at Acme Corp.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_pat_id, pn_acme_id = pn_pat.id, pn_acme.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_pat_id: _PreparedNode(
                    pn_id=pn_pat_id, pn_label="Pat", pn_node_type="Entity",
                    decision="create",
                ),
                pn_acme_id: _PreparedNode(
                    pn_id=pn_acme_id, pn_label="Acme Corp", pn_node_type="Entity",
                    decision="create",
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(session, p, plan, commit=True)
            session.commit()
            assert dec.final_status == "promoted"
            assert any(o.action == "created_new" for o in dec.edge_outcomes)
        finally:
            session.close()

        session = get_session()
        try:
            edge_evs = session.query(KGEdgeEvidence).all()
            assert len(edge_evs) == 1
            assert edge_evs[0].merge_action == "created"
            assert edge_evs[0].window_id == window_id
            assert edge_evs[0].source_text == "Pat works at Acme Corp."

            node_evs = session.query(KGNodeEvidence).all()
            assert len(node_evs) == 2  # Pat + Acme Corp both got created
            assert all(ev.merge_action == "created" for ev in node_evs)
        finally:
            session.close()

    def test_edge_match_writes_confirmed_evidence(self):
        observed_at = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())

        session = get_session()
        try:
            # Pre-seed entities + an existing edge.
            pat = Node(label="Pat", node_type="Entity", attributes={}, source="seed")
            acme = Node(label="Acme Corp", node_type="Entity", attributes={}, source="seed")
            session.add(pat)
            session.add(acme)
            session.flush()
            existing_edge = Edge(
                source_id=pat.id, target_id=acme.id,
                relationship_type="works_at", source="seed",
            )
            session.add(existing_edge)
            session.flush()
            pat_id, acme_id, existing_edge_id = pat.id, acme.id, existing_edge.id

            proposal = _seed_proposal(
                session, window_id=window_id,
                raw_text="Pat still works at Acme.",
                observed_at=observed_at,
            )
            pn_pat = _seed_pnode(session, proposal.id,
                label="Pat", node_type="Entity",
                sentence="Pat works.")
            pn_acme = _seed_pnode(session, proposal.id,
                label="Acme Corp", node_type="Entity",
                sentence="Acme Corp is a company.")
            _seed_pedge(
                session, proposal.id,
                src_pn_id=pn_pat.id, tgt_pn_id=pn_acme.id,
                predicate="works_at", sentence="Pat still works at Acme.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_pat_id, pn_acme_id = pn_pat.id, pn_acme.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_pat_id: _PreparedNode(
                    pn_id=pn_pat_id, pn_label="Pat", pn_node_type="Entity",
                    decision="match", matched_node_id=pat_id,
                ),
                pn_acme_id: _PreparedNode(
                    pn_id=pn_acme_id, pn_label="Acme Corp", pn_node_type="Entity",
                    decision="match", matched_node_id=acme_id,
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(session, p, plan, commit=True)
            session.commit()
            assert dec.final_status == "promoted"
            assert any(o.action == "matched_existing" for o in dec.edge_outcomes)
        finally:
            session.close()

        session = get_session()
        try:
            edge_evs = session.query(KGEdgeEvidence).all()
            assert len(edge_evs) == 1
            assert edge_evs[0].merge_action == "confirmed"
            assert edge_evs[0].edge_id == existing_edge_id

            node_evs = session.query(KGNodeEvidence).all()
            assert len(node_evs) == 2  # both matched
            assert all(ev.merge_action == "confirmed" for ev in node_evs)
        finally:
            session.close()


# ============================================================================
# Dry-run must NOT write evidence
# ============================================================================

class TestDryRunDoesNotWrite:

    def test_dry_run_writes_no_evidence(self):
        observed_at = datetime.now(timezone.utc)
        session = get_session()
        try:
            proposal = _seed_proposal(
                session, window_id=str(uuid.uuid4()),
                raw_text="Test.", observed_at=observed_at,
            )
            pn = _seed_pnode(
                session, proposal.id,
                label="DryRunTest", node_type="Entity", sentence="Test.",
            )
            session.commit()
            proposal_id = proposal.id
            pn_id = pn.id
        finally:
            session.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id,
            placeholder_labels=[],
            nodes={
                pn_id: _PreparedNode(
                    pn_id=pn_id, pn_label="DryRunTest", pn_node_type="Entity",
                    decision="create",
                ),
            },
        )

        session = get_session()
        try:
            p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            _evaluate_and_apply(session, p, plan, commit=False)
            session.commit()
        finally:
            session.close()

        session = get_session()
        try:
            assert session.query(KGNodeEvidence).count() == 0
            assert session.query(KGEdgeEvidence).count() == 0
        finally:
            session.close()
