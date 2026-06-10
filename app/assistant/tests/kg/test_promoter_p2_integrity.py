"""P2.2 + P2.4 + P2.5 acceptance tests (audit 2026-06-09).

P2.2 — reinforcement never regresses: last_observed is monotonic (an old
backlog window can't rewind it into decay's kill zone); a proposal with
no timestamps still stamps; the matched branch fills NULL validity
fields and folds a differing label into aliases (content change → bumps
updated_at; pure bookkeeping preserves it).

P2.4 — edge identity: employed_by collapses to works_for; the synonym
CLASS dedups and conflict-checks; symmetric / bidirectional predicates
also match the reversed triple.

P2.5 — the node_merger candidate payload carries prior distinct-verdict
memos (injected, never silently dropped).

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sql_text

from app.assistant.database.claim_proposals import ClaimProposal, ClaimProposalNode
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.predicate_vocabulary import normalize_predicate
from app.assistant.kg.proposal_promoter import (
    _call_node_merger_for_state_match,
    _existing_kg_edge,
    _is_durable_conflict,
    _refresh_on_reobservation,
)
from app.assistant.ServiceLocator.service_locator import DI
from app.models.base import get_session

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)


def _mk_node(label: str, **fields) -> str:
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type=fields.pop("node_type", "Entity"), **fields))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_edge(src: str, tgt: str, rel: str) -> str:
    eid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Edge(id=eid, source_id=src, target_id=tgt, relationship_type=rel))
        session.commit()
    finally:
        session.close()
    return eid


def _mk_proposal_with_pnode(**pnode_fields):
    session = get_session()
    try:
        p = ClaimProposal(id=str(uuid.uuid4()))
        session.add(p)
        session.flush()
        pn = ClaimProposalNode(
            proposal_id=p.id,
            label=pnode_fields.pop("label", "Test"),
            node_type=pnode_fields.pop("node_type", "State"),
            **pnode_fields,
        )
        session.add(pn)
        session.commit()
        return p.id, pn.id
    finally:
        session.close()


def _set_updated_at(nid: str, when: datetime) -> None:
    session = get_session()
    try:
        session.execute(
            sql_text("UPDATE kg_node_metadata SET updated_at = :w WHERE id = :n"),
            {"w": when, "n": nid},
        )
        session.commit()
    finally:
        session.close()


def _refresh(nid: str, proposal_id: str, pnode_id: str | None):
    """Run _refresh_on_reobservation inside one session, like the promoter."""
    session = get_session()
    try:
        node = session.query(Node).filter_by(id=nid).one()
        proposal = session.query(ClaimProposal).filter_by(id=proposal_id).one()
        pn = (
            session.query(ClaimProposalNode).filter_by(id=pnode_id).one()
            if pnode_id else None
        )
        _refresh_on_reobservation(node, proposal, proposal_node=pn)
        session.commit()
    finally:
        session.close()


def _node(nid: str) -> Node:
    session = get_session()
    try:
        n = session.query(Node).filter_by(id=nid).one()
        session.expunge(n)
        return n
    finally:
        session.close()


# ── P2.2: monotonic reinforcement ────────────────────────────────────────


def test_last_observed_never_rewinds():
    nid = _mk_node("Live State", node_type="State", last_observed=NOW,
                   observation_count=3)
    pid, pnid = _mk_proposal_with_pnode()
    session = get_session()
    try:
        session.query(ClaimProposal).filter_by(id=pid).update(
            {"last_observed_at": OLD})
        session.commit()
    finally:
        session.close()

    _refresh(nid, pid, pnid)

    after = _node(nid)
    assert after.last_observed == NOW          # old window did NOT rewind it
    assert after.observation_count == 4        # the observation still counted


def test_missing_timestamps_still_stamp():
    nid = _mk_node("Unstamped State", node_type="State", last_observed=None)
    pid, pnid = _mk_proposal_with_pnode()     # proposal has no observed_at

    before = datetime.now(timezone.utc)
    _refresh(nid, pid, pnid)

    after = _node(nid)
    assert after.last_observed is not None     # no more silent no-op
    assert after.last_observed >= before - timedelta(seconds=5)


def test_match_fills_null_dates_and_folds_alias():
    nid = _mk_node("Art Lessons", node_type="State",
                   end_date=NOW, last_dupe_scanned_at=NOW)
    _set_updated_at(nid, OLD)
    pid, pnid = _mk_proposal_with_pnode(
        label="Art Class",                     # differs → alias fold
        valid_from=OLD,                        # node NULL → fill
        valid_to=NOW - timedelta(days=1),      # node has end_date → keep node's
        valid_from_prose="since early May",
    )

    _refresh(nid, pid, pnid)

    after = _node(nid)
    assert after.start_date == OLD                       # NULL → filled
    assert after.start_date_prose == "since early May"   # NULL → filled
    assert after.end_date == NOW                         # non-NULL → untouched
    assert "Art Class" in (after.aliases or [])          # label folded in
    assert after.last_dupe_scanned_at is None            # re-pairable
    assert after.updated_at > OLD                        # content change bumped


def test_pure_bookkeeping_preserves_updated_at():
    nid = _mk_node("Stable State", node_type="State",
                   start_date=OLD, last_observed=OLD)
    _set_updated_at(nid, OLD)
    pid, pnid = _mk_proposal_with_pnode(label="Stable State")
    session = get_session()
    try:
        session.query(ClaimProposal).filter_by(id=pid).update(
            {"last_observed_at": NOW})
        session.commit()
    finally:
        session.close()

    _refresh(nid, pid, pnid)

    after = _node(nid)
    assert after.last_observed == NOW
    # No refinement happened (label equal, no proposal dates) → updated_at
    # preserved verbatim, no wiki/card refresh cascade.
    assert after.updated_at == OLD


# ── P2.4: edge identity ──────────────────────────────────────────────────


def test_employed_by_normalizes_to_works_for():
    assert normalize_predicate("employed_by") == ("works_for", True)
    assert normalize_predicate("Employed_By") == ("works_for", True)


def test_existing_edge_matches_synonym_class():
    a, b = _mk_node("Person"), _mk_node("Company")
    _mk_edge(a, b, "works_for")
    session = get_session()
    try:
        assert _existing_kg_edge(session, a, b, "employed_by") is not None
    finally:
        session.close()


def test_existing_edge_reversed_for_symmetric_predicate():
    # Symmetry is DATA, not code: edge_canon.is_symmetric (curated by the
    # edge_canon_curator agent) drives the reversed-triple check. Seed the
    # canon row the way the curator would, then reset the read cache.
    from app.assistant.kg.db.knowledge_graph_db_sqlite import EdgeCanon
    from app.assistant.kg.predicate_vocabulary import reset_db_alias_cache

    session = get_session()
    try:
        if session.query(EdgeCanon).filter_by(edge_type="is_sibling_in").count() == 0:
            session.add(EdgeCanon(
                edge_type="is_sibling_in", domain_type="Entity",
                range_type="Entity", is_symmetric=True,
            ))
            session.commit()
    finally:
        session.close()
    reset_db_alias_cache()

    a, b = _mk_node("Sib1"), _mk_node("Sib2")
    _mk_edge(a, b, "is_sibling_in")
    session = get_session()
    try:
        # Reversed re-assertion of a symmetric predicate finds the edge…
        assert _existing_kg_edge(session, b, a, "is_sibling_in") is not None
        # …a directional predicate doesn't, unless flagged bidirectional.
        _mk_edge(a, b, "works_for")
        assert _existing_kg_edge(session, b, a, "works_for") is None
        assert _existing_kg_edge(
            session, b, a, "works_for", bidirectional=True) is not None
    finally:
        session.close()


def test_durable_conflict_checks_synonym_class_and_normalizes():
    person, employer, other = _mk_node("P"), _mk_node("E1"), _mk_node("E2")
    _mk_edge(person, employer, "works_for")
    spouse_a, spouse_b, spouse_c = _mk_node("S1"), _mk_node("S2"), _mk_node("S3")
    _mk_edge(spouse_a, spouse_b, "is_spouse_in")

    session = get_session()
    try:
        # employed_by (synonym) conflicts with the existing works_for edge.
        hit = _is_durable_conflict(session, person, "employed_by", other)
        assert hit is not None and hit.target_id == employer
        # Same target is no conflict (reinforcement path).
        assert _is_durable_conflict(session, person, "employed_by", employer) is None
        # Raw is_married normalizes to is_spouse_in and trips the check.
        hit = _is_durable_conflict(session, spouse_a, "is_married", spouse_c)
        assert hit is not None and hit.target_id == spouse_b
    finally:
        session.close()


# ── P2.5: verdict memos reach the node_merger ────────────────────────────


def test_merger_payload_gets_distinct_verdict_notes(monkeypatch):
    a, b = sorted([_mk_node("Twin1"), _mk_node("Twin2")])
    session = get_session()
    try:
        session.add(KGNodeVerdict(
            node_id_a=a, node_id_b=b, verdict_type="distinct",
            memo="kept separate — different eras", decided_by="agent:kg_investigation",
        ))
        session.commit()
    finally:
        session.close()

    captured = {}

    class _FakeMerger:
        def action_handler(self, msg):
            captured.update(msg.agent_input or {})
            return SimpleNamespace(data={"merge_nodes": False})

    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: _FakeMerger())

    candidates = [{"node_id": a, "label": "Twin1"}, {"node_id": b, "label": "Twin2"}]
    result = _call_node_merger_for_state_match({"label": "new obs"}, candidates)
    assert result is None

    import json
    payload = json.loads(captured["existing_node_candidates"])
    notes_a = payload[0].get("prior_distinct_verdicts")
    notes_b = payload[1].get("prior_distinct_verdicts")
    assert notes_a and "DISTINCT" in notes_a[0]
    assert notes_b and "kept separate" in notes_b[0]
