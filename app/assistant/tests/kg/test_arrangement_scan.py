"""Arrangement supersession (2026-10-05).

A friend's card said "Thursday evening Zoom" for ten months after the call moved to Mondays: nothing in the KG closes
an open State that a later statement replaced. Pins: candidates are open States with a later-said State/Event sharing
a participant other than the owner (said dates from the source messages, not first_observed); a supersedes verdict
becomes a finding born investigated / needs_user_review whose recommendation names kg_close_state and the
succeeded_by edge; other verdicts are cooldown stamps; a malformed verdict raises.

Uses the kg conftest (isolated test DB, fresh tables per test). Invented data only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.pipelines.kg_maintenance_pipeline import step_arrangement_scan as S
from app.models.base import get_session

D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731
NOW = D(2026, 10, 5)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PRIMARY_USER", "Sam")
    monkeypatch.setattr(S, "_embed", lambda texts: {})
    monkeypatch.setattr(S, "_similarity", lambda text, others, vectors: [1.0 / (i + 1) for i in range(len(others))])
    s = get_session()
    try:
        s.query(KGMaintenanceFinding).delete()
        s.commit()
    finally:
        s.close()


def _node(label, node_type="State", said=(), **fields):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type=node_type, original_sentence=fields.pop("sentence", label), **fields))
        for d in said:
            s.add(KGNodeEvidence(node_id=nid, source_table="unified_log_2026", message_timestamp=d, source_text=label))
        s.commit()
    finally:
        s.close()
    return nid


def _edge(a, b, rel="participant"):
    s = get_session()
    try:
        s.add(Edge(id=str(uuid.uuid4()), source_id=a, target_id=b, relationship_type=rel))
        s.commit()
    finally:
        s.close()


def _world():
    owner, pat, kim = _node("Sam", "Entity"), _node("Pat", "Entity"), _node("Kim", "Entity")
    thursday = _node("Recurring Call", said=[D(2025, 1, 2)], first_observed=D(2026, 5, 12),
                     sentence="Sam and Pat have a call every Thursday at 8pm.", importance=5.0)
    monday = _node("Monday Call", said=[D(2026, 3, 3)], sentence="The call with Pat is on Mondays at 8.")
    earlier = _node("Chess", said=[D(2024, 6, 1)], sentence="Pat and Sam play chess.")
    owner_only = _node("Sam's Routine", said=[D(2026, 4, 1)], sentence="Sam walks the dogs at 9.")
    kim_fact = _node("Kim's Job", said=[D(2026, 5, 1)], sentence="Kim works at the bakery.")
    for claim in (thursday, monday, earlier, owner_only):
        _edge(owner, claim, "has_state")
    for claim in (thursday, monday, earlier):
        _edge(claim, pat)
    _edge(kim, kim_fact, "has_state")
    return SimpleNamespace(thursday=thursday, monday=monday, earlier=earlier, owner_only=owner_only)


def _gather():
    s = get_session()
    try:
        return S.gather(s, now=NOW)
    finally:
        s.close()


def test_candidates_share_a_participant_other_than_the_owner_and_are_said_later():
    w = _world()
    inputs = _gather()
    by_older = {i["older"]["node_id"]: [n["node_id"] for n in i["newer"]] for i in inputs}
    assert by_older[w.thursday] == [w.monday], "later and sharing Pat; the owner's own routine shares only the owner"
    assert set(by_older[w.earlier]) == {w.thursday, w.monday}
    thursday = next(i for i in inputs if i["older"]["node_id"] == w.thursday)
    assert thursday["older"]["said"] == "2025-01-02", "the message date, not first_observed (the import date)"
    assert thursday["older"]["participants"] == ["Pat"]


def test_a_supersedes_verdict_becomes_a_finding_for_review_naming_the_calls(monkeypatch):
    w = _world()

    def judge(ji):
        if ji["older"]["node_id"] == w.thursday:
            return {"verdict": "supersedes", "superseded_by": w.monday, "change_date": "2026-03-03",
                    "change_date_basis": "the Monday statement", "confidence": "high", "reasoning": "moved"}
        return {"verdict": "both_true", "confidence": "high", "reasoning": "different things"}
    monkeypatch.setattr(S, "_run_judge", judge)
    out = S.run(SimpleNamespace(run_id="r1"))
    assert out["supersession_findings"] == 1 and out["cooled"] == out["candidates"] - 1
    s = get_session()
    try:
        [f] = [f for f in s.query(KGMaintenanceFinding).filter_by(status="investigated").all()
               if f.evidence_json["older_id"] == w.thursday]
        assert {f.primary_node_id, f.secondary_node_id} == {w.thursday, w.monday}, "the pair, stored sorted by id"
        rec = f.investigation_report_json["recommendation"]
        assert f.status == "investigated" and f.investigation_report_json["disposition"] == "needs_user_review"
        assert f"kg_close_state(node_id={w.thursday!r}, end_date='2026-03-03'" in rec
        assert "relationship_type='succeeded_by'" in rec
        stamps = s.query(KGMaintenanceFinding).filter_by(status="rejected").count()
        assert stamps == out["cooled"]
    finally:
        s.close()
    assert w.thursday not in {i["older"]["node_id"] for i in _gather()}, "an open finding keeps it out of the scan"


def test_a_malformed_verdict_raises(monkeypatch):
    w = _world()
    monkeypatch.setattr(S, "_run_judge", lambda ji: {"verdict": "supersedes", "superseded_by": "elsewhere",
                                                     "change_date": "2026-03-03", "confidence": "high",
                                                     "reasoning": "x"})
    with pytest.raises(ValueError, match="not one of the later statements"):
        S.run(SimpleNamespace(run_id="r1"))
    monkeypatch.setattr(S, "_run_judge", lambda ji: {"verdict": "maybe", "confidence": "high", "reasoning": "x"})
    with pytest.raises(ValueError, match="is not one of"):
        S.run(SimpleNamespace(run_id="r1"))


def test_a_state_is_judged_again_only_when_a_new_later_statement_appears(monkeypatch):
    w = _world()
    monkeypatch.setattr(S, "_run_judge", lambda ji: {"verdict": "unrelated", "confidence": "high", "reasoning": "x"})
    first = S.run(SimpleNamespace(run_id="r1"))
    assert first["judged"] >= 1 and S.run(SimpleNamespace(run_id="r2"))["judged"] == 0, "nothing new: no re-judging"
    newest = _node("Call Moved", said=[D(2026, 9, 22)], sentence="The call with Pat starts at 8:30 now.")
    _edge(newest, _gather_pat())
    again = {i["older"]["node_id"] for i in _gather()}
    assert w.thursday in again, "a new later statement sharing Pat brings it back"


def _gather_pat():
    s = get_session()
    try:
        return s.query(Node).filter(Node.label == "Pat").one().id
    finally:
        s.close()

