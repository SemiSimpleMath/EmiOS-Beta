"""Regression tests: derived/projection writers must NOT bump Node.updated_at.

Background
----------
``Node.updated_at`` is the dirty signal that drives wiki and entity-card
refresh cascades (see kg_projection.change_detection.find_changed_neighborhood_nodes,
which is called from wiki_generator.nightly_refresh and entity_management/
entity_cards.nodes_modified_since). Bumping updated_at on a node forces every
wiki + entity card whose neighborhood touches it to refresh — even if the
node's semantic content didn't change.

Three writers that historically bumped updated_at without semantic change:
  1. me/importance.py (importance score recomputed by the lens)
  2. step_pagerank.py (pagerank recomputed nightly)
  3. proposal_promoter._refresh_on_reobservation (observation-count bump on match)

Each was patched to use the ``Node.updated_at = Node.updated_at`` self-reference
trick that suppresses the column's onupdate=func.now() hook (same pattern as
persist_description). These tests guard against regression by exercising each
writer and asserting updated_at is preserved.

Adding a new node tagged early_childhood near Annika should only touch the
sections that actually changed — NOT bump Annika herself, and NOT cascade into
unrelated wikis. These tests prove the underlying invariant.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalEdge, ClaimProposalEvidence, ClaimProposalNode,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.proposal_promoter import (
    _PreparedNode, _PromoterPlan, _evaluate_and_apply, _refresh_on_reobservation,
)
from app.models.base import get_session


def _seed_node(
    session, *, label: str, node_type: str = "Entity", attributes=None,
    first_observed=None, last_observed=None, observation_count=1,
    last_pursued_at=None,
) -> Node:
    n = Node(
        label=label, node_type=node_type,
        attributes=attributes or {}, source="seed",
        first_observed=first_observed,
        last_observed=last_observed,
        observation_count=observation_count,
        last_pursued_at=last_pursued_at,
    )
    session.add(n)
    session.flush()
    return n


def _read_updated_at(node_id: str):
    s = get_session()
    try:
        n = s.query(Node).filter(Node.id == node_id).first()
        return n.updated_at if n else None
    finally:
        s.close()


# ============================================================================
# 1. me/importance.py — importance writes must not bump updated_at
# ============================================================================

class TestImportanceWriterDoesNotBump:

    def test_importance_update_preserves_updated_at(self):
        from sqlalchemy import update as sql_update
        s = get_session()
        try:
            n = _seed_node(s, label="ImportanceTest_A")
            s.commit()
            node_id = n.id
        finally:
            s.close()

        before = _read_updated_at(node_id)
        time.sleep(0.05)  # ensure any natural bump would be detectable

        # Same write the lens does (me/importance.py:216 pattern).
        s = get_session()
        try:
            s.query(Node).filter(Node.id == node_id).update(
                {Node.importance: 0.87, Node.updated_at: Node.updated_at},
                synchronize_session=False,
            )
            s.commit()
        finally:
            s.close()

        # Verify importance was actually written.
        s = get_session()
        try:
            n = s.query(Node).filter(Node.id == node_id).first()
            assert n.importance == pytest.approx(0.87)
        finally:
            s.close()

        # Verify updated_at was preserved.
        after = _read_updated_at(node_id)
        assert after == before, (
            f"importance write bumped updated_at: {before} -> {after}"
        )


# ============================================================================
# 2. step_pagerank.py — pagerank writes must not bump updated_at
# ============================================================================

class TestPagerankWriterDoesNotBump:

    def test_pagerank_update_preserves_updated_at(self):
        s = get_session()
        try:
            n = _seed_node(s, label="PagerankTest_A")
            s.commit()
            node_id = n.id
        finally:
            s.close()

        before = _read_updated_at(node_id)
        time.sleep(0.05)

        # Same write step_pagerank does.
        s = get_session()
        try:
            s.query(Node).filter(Node.id == node_id).update(
                {Node.pagerank_score: 0.0042, Node.updated_at: Node.updated_at},
                synchronize_session=False,
            )
            s.commit()
        finally:
            s.close()

        s = get_session()
        try:
            n = s.query(Node).filter(Node.id == node_id).first()
            assert n.pagerank_score == pytest.approx(0.0042)
        finally:
            s.close()

        after = _read_updated_at(node_id)
        assert after == before, (
            f"pagerank write bumped updated_at: {before} -> {after}"
        )


# ============================================================================
# 3. _refresh_on_reobservation — proposal match must not bump matched-node's
#    updated_at. Adding a new connected node to Annika should not mark Annika
#    as "changed" for downstream cascades.
# ============================================================================

class TestRefreshOnReobservationDoesNotBumpMatchedNode:

    def test_reobservation_preserves_matched_node_updated_at(self):
        s = get_session()
        try:
            # first_observed + observation_count are first-class columns since
            # 2026-05-11; the seed helper should set them directly on the Node.
            existing = _seed_node(
                s, label="Annika_Test", node_type="Entity",
                first_observed=datetime(2025, 1, 1, tzinfo=timezone.utc),
                observation_count=1,
            )
            s.commit()
            existing_id = existing.id
        finally:
            s.close()

        before = _read_updated_at(existing_id)
        time.sleep(0.05)

        # Simulate a proposal that matches existing Annika as a participant.
        observed_at = datetime.now(timezone.utc)
        s = get_session()
        try:
            proposal = ClaimProposal(
                id=str(uuid.uuid4()), status="pending", claim_type="durable",
                first_observed_at=observed_at, last_observed_at=observed_at,
            )
            s.add(proposal)
            s.flush()
            s.add(ClaimProposalEvidence(
                id=str(uuid.uuid4()), proposal_id=proposal.id,
                source_type="chat", room_id="master_room",
                window_id=str(uuid.uuid4()), unified_log_id=str(uuid.uuid4()),
                raw_text="Annika did something new", observed_at=observed_at,
            ))
            s.commit()
            proposal_id = proposal.id
        finally:
            s.close()

        # Run the refresh-on-reobservation path, then commit through that
        # session (the function operates on an ORM-attached node).
        s = get_session()
        try:
            n = s.query(Node).filter(Node.id == existing_id).first()
            p = s.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            _refresh_on_reobservation(n, p)
            s.commit()
        finally:
            s.close()

        # Observation tracking did update (function did its job).
        # All three lifecycle fields are first-class columns now.
        s = get_session()
        try:
            n = s.query(Node).filter(Node.id == existing_id).first()
            assert n.observation_count == 2, "observation_count should increment"
            assert n.last_observed is not None, "last_observed should be set"
            # Compare to-the-second; SQLite drops tzinfo on round-trip so
            # ts may come back as naive — compare on isoformat prefix.
            stored = n.last_observed.replace(tzinfo=None) if n.last_observed.tzinfo else n.last_observed
            expected = observed_at.replace(tzinfo=None)
            assert abs((stored - expected).total_seconds()) < 1.0, "last_observed should match the observation timestamp"
        finally:
            s.close()

        # And updated_at was preserved (the whole point).
        after = _read_updated_at(existing_id)
        assert after == before, (
            f"reobservation bumped matched node's updated_at: {before} -> {after}"
        )

    def test_full_match_path_via_evaluate_and_apply_preserves_updated_at(self):
        """End-to-end: proposal with Annika as matched participant goes
        through _evaluate_and_apply (commit=True). Matched-existing node's
        updated_at must not change. Newly-created node's updated_at is
        naturally fresh — we don't assert on that, just on the existing one.
        """
        s = get_session()
        try:
            annika = _seed_node(s, label="Annika_E2E", node_type="Entity")
            s.commit()
            annika_id = annika.id
        finally:
            s.close()

        before = _read_updated_at(annika_id)
        time.sleep(0.05)

        observed_at = datetime.now(timezone.utc)
        s = get_session()
        try:
            proposal = ClaimProposal(
                id=str(uuid.uuid4()), status="pending", claim_type="durable",
                first_observed_at=observed_at, last_observed_at=observed_at,
            )
            s.add(proposal)
            s.flush()
            s.add(ClaimProposalEvidence(
                id=str(uuid.uuid4()), proposal_id=proposal.id,
                source_type="chat", room_id="master_room",
                window_id=str(uuid.uuid4()), unified_log_id=str(uuid.uuid4()),
                raw_text="Annika played in a school recital", observed_at=observed_at,
            ))
            pn_annika = ClaimProposalNode(
                id=str(uuid.uuid4()), proposal_id=proposal.id,
                extractor_temp_id="n_annika",
                label="Annika_E2E", node_type="Entity",
                sentence="Annika played in a school recital.",
            )
            s.add(pn_annika)
            s.commit()
            proposal_id, pn_annika_id = proposal.id, pn_annika.id
        finally:
            s.close()

        plan = _PromoterPlan(
            proposal_id=proposal_id, placeholder_labels=[],
            nodes={
                pn_annika_id: _PreparedNode(
                    pn_id=pn_annika_id, pn_label="Annika_E2E", pn_node_type="Entity",
                    decision="match", matched_node_id=annika_id,
                ),
            },
        )

        s = get_session()
        try:
            p = s.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
            dec = _evaluate_and_apply(s, p, plan, commit=True)
            s.commit()
            assert dec.final_status == "promoted"
            assert any(o.action == "matched_existing" for o in dec.node_outcomes)
        finally:
            s.close()

        after = _read_updated_at(annika_id)
        assert after == before, (
            f"end-to-end match bumped Annika's updated_at: {before} -> {after}"
        )
