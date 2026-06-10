"""P2.1 acceptance tests (audit 2026-06-09) — temporally-aware
single-target contradiction, conservative path (P4.4 decision 2026-06-10).

The old behavior: any second assertion of a single-target predicate
(spouse, employer, birthplace…) auto-`contradicted` the WHOLE proposal
group forever — the KG froze on first-asserted values for exactly the
facts that change in real life, and `contradicted` was a dead end nothing
ever read.

New behavior, verified here on the live spouse shape (person →
Marriage-State edges):
  - CLOSED ERA: existing era has end_date → not a conflict; the successor
    edge is created (remarriage after a closed marriage).
  - SUCCESSION: era still open, proposal dates postdate its start → the
    proposal is HELD (final_status='held', stays pending upstream) and a
    single_target_succession finding routes the close-the-era question to
    the user. Finding references only pre-existing node ids.
  - SAME ERA: undatable double assertion → only the conflicting edge is
    skipped (rest of the group applies), with a single_target_conflict
    finding raised.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.claim_proposals import (
    ClaimProposal,
    ClaimProposalEdge,
    ClaimProposalEvidence,
    ClaimProposalNode,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.proposal_promoter import (
    _PreparedNode,
    _PromoterPlan,
    _classify_durable_conflict,
    _evaluate_and_apply,
)
from app.models.base import get_session

T2003 = datetime(2003, 9, 9, tzinfo=timezone.utc)
T2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
T2026 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── scenario builder ─────────────────────────────────────────────────────


def _seed_world(session, *, old_era_start, old_era_end):
    """Existing KG: person P married into Marriage-State M_old."""
    person = Node(id=str(uuid.uuid4()), label="Pat", node_type="Entity")
    m_old = Node(
        id=str(uuid.uuid4()), label="Marriage", node_type="State",
        start_date=old_era_start, end_date=old_era_end,
    )
    session.add_all([person, m_old])
    session.flush()
    session.add(Edge(
        id=str(uuid.uuid4()), source_id=person.id, target_id=m_old.id,
        relationship_type="is_spouse_in", sentence="Pat is married.",
    ))
    session.flush()
    return person.id, m_old.id


def _seed_proposal(session, *, new_era_start, extra_edge: bool = False):
    """Proposal: Pat (match) + new Marriage State (create, valid_from=
    new_era_start) + edge Pat → new Marriage is_spouse_in. With
    ``extra_edge``, also an unrelated Hobby state + edge so we can verify
    the rest of the group applies."""
    p = ClaimProposal(
        id=str(uuid.uuid4()), status="pending", claim_type="durable",
        representative_sentence="Pat got married again.",
        first_observed_at=T2026, last_observed_at=T2026,
    )
    session.add(p)
    session.flush()
    session.add(ClaimProposalEvidence(
        id=str(uuid.uuid4()), proposal_id=p.id, source_type="chat",
        raw_text="Pat got married again.", observed_at=T2026,
        window_id=str(uuid.uuid4()),
    ))

    pn_person = ClaimProposalNode(
        id=str(uuid.uuid4()), proposal_id=p.id, label="Pat",
        node_type="Entity", sentence="Pat is a person.",
    )
    pn_marriage = ClaimProposalNode(
        id=str(uuid.uuid4()), proposal_id=p.id, label="Marriage",
        node_type="State", sentence="Pat is married to Sky.",
        valid_from=new_era_start,
    )
    session.add_all([pn_person, pn_marriage])
    session.flush()
    session.add(ClaimProposalEdge(
        id=str(uuid.uuid4()), proposal_id=p.id,
        source_node_id=pn_person.id, target_node_id=pn_marriage.id,
        predicate="is_spouse_in", sentence="Pat is married to Sky.",
    ))
    pn_hobby = None
    if extra_edge:
        pn_hobby = ClaimProposalNode(
            id=str(uuid.uuid4()), proposal_id=p.id, label="Hiking",
            node_type="State", sentence="Pat likes hiking.",
        )
        session.add(pn_hobby)
        session.flush()
        session.add(ClaimProposalEdge(
            id=str(uuid.uuid4()), proposal_id=p.id,
            source_node_id=pn_person.id, target_node_id=pn_hobby.id,
            predicate="has_state", sentence="Pat likes hiking.",
        ))
    session.flush()
    return p, pn_person, pn_marriage, pn_hobby


def _plan_for(p, pn_person, pn_marriage, person_kg_id, pn_hobby=None):
    nodes = {
        pn_person.id: _PreparedNode(
            pn_id=pn_person.id, pn_label="Pat", pn_node_type="Entity",
            decision="match", matched_node_id=person_kg_id,
        ),
        pn_marriage.id: _PreparedNode(
            pn_id=pn_marriage.id, pn_label="Marriage", pn_node_type="State",
            decision="create",
        ),
    }
    if pn_hobby is not None:
        nodes[pn_hobby.id] = _PreparedNode(
            pn_id=pn_hobby.id, pn_label="Hiking", pn_node_type="State",
            decision="create",
        )
    return _PromoterPlan(proposal_id=p.id, nodes=nodes)


def _run_scenario(*, old_era_start, old_era_end, new_era_start,
                  extra_edge: bool = False):
    session = get_session()
    try:
        person_id, m_old_id = _seed_world(
            session, old_era_start=old_era_start, old_era_end=old_era_end)
        p, pn_person, pn_marriage, pn_hobby = _seed_proposal(
            session, new_era_start=new_era_start, extra_edge=extra_edge)
        plan = _plan_for(p, pn_person, pn_marriage, person_id, pn_hobby)
        session.commit()

        dec = _evaluate_and_apply(session, p, plan, commit=True)
        session.commit()
        return session, dec, person_id, m_old_id
    except Exception:
        session.close()
        raise


# ── the three temporal outcomes ──────────────────────────────────────────


def test_closed_era_allows_successor_edge():
    session, dec, person_id, m_old_id = _run_scenario(
        old_era_start=T2003, old_era_end=T2024,   # old marriage CLOSED
        new_era_start=T2026,
    )
    try:
        assert dec.final_status == "promoted"
        assert dec.followup_findings == []
        # The new is_spouse_in edge exists alongside the historical one.
        spouse_edges = (
            session.query(Edge)
            .filter(Edge.source_id == person_id,
                    Edge.relationship_type == "is_spouse_in")
            .all()
        )
        assert len(spouse_edges) == 2
    finally:
        session.close()


def test_open_era_with_later_dates_holds_as_succession():
    session, dec, person_id, m_old_id = _run_scenario(
        old_era_start=T2003, old_era_end=None,    # old marriage still OPEN
        new_era_start=T2026,
    )
    try:
        assert dec.final_status == "held"
        assert len(dec.followup_findings) == 1
        f = dec.followup_findings[0]
        assert f["finding_type"] == "single_target_succession"
        # References only PRE-EXISTING ids (the new node rolls back upstream).
        assert f["primary_node_id"] == person_id
        assert f["secondary_node_id"] == m_old_id
        assert f["evidence"]["proposed_start"] == str(T2026)
    finally:
        session.close()


def test_same_era_skips_edge_but_applies_rest_of_group():
    session, dec, person_id, m_old_id = _run_scenario(
        old_era_start=None, old_era_end=None,     # no dates anywhere
        new_era_start=None,
        extra_edge=True,
    )
    try:
        assert dec.final_status == "promoted"      # NOT contradicted
        assert len(dec.followup_findings) == 1
        assert dec.followup_findings[0]["finding_type"] == "single_target_conflict"

        # The conflicting spouse edge was skipped at EDGE granularity…
        spouse_edges = (
            session.query(Edge)
            .filter(Edge.source_id == person_id,
                    Edge.relationship_type == "is_spouse_in")
            .all()
        )
        assert len(spouse_edges) == 1              # only the original
        skipped = [o for o in dec.edge_outcomes if o.action == "skipped_conflict"]
        assert len(skipped) == 1

        # …while the unrelated edge in the same group applied.
        hobby_edges = (
            session.query(Edge)
            .filter(Edge.source_id == person_id,
                    Edge.relationship_type == "has_state")
            .all()
        )
        assert len(hobby_edges) == 1
    finally:
        session.close()


# ── classifier unit coverage (incl. the employment shape) ───────────────


def test_classifier_employment_shape_uses_shared_state_era():
    """works_for edges run State → employer; the era lives on the SHARED
    source State. Closed state → closed_era."""
    session = get_session()
    try:
        emp_state = Node(id=str(uuid.uuid4()), label="Employment",
                         node_type="State", start_date=T2003, end_date=T2024)
        old_co = Node(id=str(uuid.uuid4()), label="OldCo", node_type="Entity")
        session.add_all([emp_state, old_co])
        session.flush()
        old_edge = Edge(id=str(uuid.uuid4()), source_id=emp_state.id,
                        target_id=old_co.id, relationship_type="works_for")
        session.add(old_edge)
        session.flush()

        kind, era = _classify_durable_conflict(session, old_edge, emp_state.id, T2026)
        assert kind == "closed_era"
        assert era.id == emp_state.id
    finally:
        session.rollback()
        session.close()


def test_classifier_no_dates_is_same_era():
    session = get_session()
    try:
        person = Node(id=str(uuid.uuid4()), label="P", node_type="Entity")
        m = Node(id=str(uuid.uuid4()), label="Marriage", node_type="State")
        session.add_all([person, m])
        session.flush()
        old_edge = Edge(id=str(uuid.uuid4()), source_id=person.id,
                        target_id=m.id, relationship_type="is_spouse_in")
        session.add(old_edge)
        session.flush()

        kind, _ = _classify_durable_conflict(session, old_edge, person.id, None)
        assert kind == "same_era"
        # Dates on the incoming side alone don't make a succession either —
        # without an existing-era start there's no order to establish.
        kind, _ = _classify_durable_conflict(session, old_edge, person.id, T2026)
        assert kind == "same_era"
    finally:
        session.rollback()
        session.close()
