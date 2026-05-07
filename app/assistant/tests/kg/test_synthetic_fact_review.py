"""Tests for the synthetic_fact_review module.

The module owns the user-in-the-loop state machine for facts that
agents inferred but need human review before landing in the KG.
Producers today: wiki_connection_investigator. The architecture
generalizes: any future producer (graph-walker, missing-connection
finder, etc.) writes the same finding_type='synthetic_fact_proposal'
shape and reuses this review path.
"""
from __future__ import annotations

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.synthetic_fact_review.store import (
    FINDING_TYPE,
    REVIEW_STATUSES,
    apply_user_edit,
    get_review_state,
    list_pending_review,
    mark_approved,
    mark_rejected,
)
from app.models.base import Base, get_session


JUKKA_ID = "11111111-jukk-jukk-jukk-111111111111"


@pytest.fixture(autouse=True)
def _seed(kg_clean_db):
    session = get_session()
    engine = session.bind
    session.close()
    KGMaintenanceFinding.__table__.drop(engine, checkfirst=True)
    KGMaintenanceFinding.__table__.create(engine, checkfirst=True)
    Base.metadata.create_all(engine)

    session = get_session()
    try:
        session.add(Node(id=JUKKA_ID, label="Jukka", node_type="Person"))
        session.commit()
    finally:
        session.close()


_PROPOSAL_COUNTER = [0]


def _make_proposal(
    *,
    sentence: str = "Dylan is Jukka's nephew.",
    confidence: float = 0.9,
    suggested_start_date: str | None = None,
    producer: str = "wiki_connection_investigator",
) -> str:
    """Create a synthetic_fact_proposal finding. upsert_finding dedups
    on (type, primary, secondary) — to make multiple findings on the
    same primary in tests, we vary the secondary with a counter. The
    secondary string sorts above the primary so pair-normalization
    leaves primary in place."""
    _PROPOSAL_COUNTER[0] += 1
    fake_secondary = f"zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzz{_PROPOSAL_COUNTER[0]:04d}"
    fid, _ = upsert_finding(
        finding_type=FINDING_TYPE,
        primary_node_id=JUKKA_ID,
        secondary_node_id=fake_secondary,
        suggested_action="review",
        reason=f"Inferred: {sentence}",
        confidence=confidence,
        priority="low",
        agent_name=producer,
        evidence={
            "producer": producer,
            "sentence": sentence,
            "sentence_hash": f"hash{_PROPOSAL_COUNTER[0]}",
            "evidence_quote": "test quote",
            "inference_path": "test inference",
            "agent_confidence": confidence,
            "subject_label": "Jukka",
            "subject_node_type": "Person",
            "suggested_start_date": suggested_start_date,
            "suggested_end_date": None,
            "review_status": "pending_review",
        },
    )
    return fid


# ── Tests ─────────────────────────────────────────────────────────────


def test_get_review_state_returns_full_shape():
    fid = _make_proposal(sentence="Dylan is Jukka's nephew.")
    state = get_review_state(fid)
    assert state is not None
    assert state["finding_id"] == fid
    assert state["producer"] == "wiki_connection_investigator"
    assert state["agent_sentence"] == "Dylan is Jukka's nephew."
    assert state["effective_sentence"] == "Dylan is Jukka's nephew."
    assert state["review_status"] == "pending_review"


def test_get_review_state_returns_none_for_wrong_type():
    fid, _ = upsert_finding(
        finding_type="duplicate_node",
        primary_node_id=JUKKA_ID,
        suggested_action="merge",
        priority="medium",
        agent_name="other",
    )
    assert get_review_state(fid) is None


def test_get_review_state_returns_none_for_missing():
    assert get_review_state("does-not-exist") is None


def test_apply_user_edit_advances_status_and_persists():
    fid = _make_proposal()
    state = apply_user_edit(
        fid,
        edited_sentence="Dylan is Jukka's only nephew.",
        start_date="2018-01-01",
        notes="Confirmed with Diana over the phone.",
    )
    assert state["review_status"] == "edited"
    assert state["user_edited_sentence"] == "Dylan is Jukka's only nephew."
    assert state["user_start_date"] == "2018-01-01"
    assert state["user_notes"] == "Confirmed with Diana over the phone."
    # Effective values prefer user input over agent suggestion.
    assert state["effective_sentence"] == "Dylan is Jukka's only nephew."
    assert state["effective_start_date"] == "2018-01-01"


def test_apply_user_edit_empty_string_clears_field():
    """An empty string means 'clear this field'; None means 'leave alone'."""
    fid = _make_proposal()
    apply_user_edit(fid, edited_sentence="rewritten", notes="some notes")
    apply_user_edit(fid, notes="")  # clear notes; sentence untouched
    state = get_review_state(fid)
    assert state["user_edited_sentence"] == "rewritten"
    assert state["user_notes"] is None


def test_apply_user_edit_preserves_approved_status():
    """Once a user has approved, a subsequent edit shouldn't demote
    them back to 'edited'. The state machine moves forward."""
    fid = _make_proposal()
    apply_user_edit(fid, edited_sentence="x")
    mark_approved(fid)
    state_before = get_review_state(fid)
    assert state_before["review_status"] == "approved"

    apply_user_edit(fid, edited_sentence="y")
    state_after = get_review_state(fid)
    assert state_after["review_status"] == "approved"
    assert state_after["user_edited_sentence"] == "y"


def test_mark_approved_advances_status():
    fid = _make_proposal()
    state = mark_approved(fid)
    assert state["review_status"] == "approved"


def test_mark_rejected_closes_underlying_finding():
    """Reject must flip BOTH the review_status AND the finding.status,
    so rejected findings don't keep showing up on the main dashboard."""
    fid = _make_proposal()
    mark_rejected(fid)

    session = get_session()
    try:
        row = session.query(KGMaintenanceFinding).filter_by(id=fid).first()
    finally:
        session.close()
    assert row.status == "rejected"
    ev = row.evidence_json
    assert ev["review_status"] == "rejected"


def test_list_pending_review_filters_to_pending_and_edited():
    """The review inbox shows only findings the user hasn't acted on
    yet — pending_review (untouched) + edited (in-progress)."""
    fid_pending = _make_proposal(sentence="Fact A.")
    fid_edited = _make_proposal(sentence="Fact B.")
    fid_approved = _make_proposal(sentence="Fact C.")
    fid_rejected = _make_proposal(sentence="Fact D.")

    apply_user_edit(fid_edited, notes="working on it")
    mark_approved(fid_approved)
    mark_rejected(fid_rejected)

    inbox = list_pending_review(limit=100)
    inbox_ids = {f["id"] for f in inbox}
    assert fid_pending in inbox_ids
    assert fid_edited in inbox_ids
    assert fid_approved not in inbox_ids
    assert fid_rejected not in inbox_ids
    # review_status surfaces on the listed dicts so the UI can render
    # an "untouched" vs "in progress" badge.
    statuses = {f["id"]: f["review_status"] for f in inbox}
    assert statuses[fid_pending] == "pending_review"
    assert statuses[fid_edited] == "edited"


def test_review_statuses_constant_complete():
    """Sanity: every status name the module sets is in REVIEW_STATUSES."""
    expected = {"pending_review", "edited", "approved", "rejected"}
    assert expected.issubset(set(REVIEW_STATUSES))


def test_apply_user_edit_raises_on_wrong_type():
    fid, _ = upsert_finding(
        finding_type="duplicate_node",
        primary_node_id=JUKKA_ID,
        suggested_action="merge",
        priority="medium",
        agent_name="x",
    )
    with pytest.raises(ValueError):
        apply_user_edit(fid, edited_sentence="x")


def test_producer_field_passes_through():
    """A future graph-walker producer can write to the same finding_type
    and the review state preserves which producer made the inference."""
    fid = _make_proposal(producer="missing_connection_walker")
    state = get_review_state(fid)
    assert state["producer"] == "missing_connection_walker"
