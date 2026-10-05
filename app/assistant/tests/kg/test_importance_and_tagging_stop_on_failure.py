"""The importance rater and section tagger stop at a failed call (owner, 2026-10-05).

Edge rater: a score of 0 is a 0; an edge left unrated or given a non-numeric score gets ONE
correction listing exactly those ids, and is never written as a default; a failed call stops the
run with nothing of the batch written. Section tagger: an omitted node gets one correction and is
never marked processed; a failed call or write stops the run. Every failure is counted in the
failure ledger against the items it concerns, and set-aside items are skipped. The
kg_importance_rater routine lets each failure escape, so the runner records it.

Uses the kg conftest (isolated test DB). Agents are fakes, so no LLM.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.importance import scoring
from app.assistant.kg import failure_ledger, section_tagging
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node, NodeSectionTag
from app.models.base import get_session


@pytest.fixture(autouse=True)
def _clean_ledger_and_tags():
    s = get_session()
    try:
        failure_ledger._ensure(s)
        s.query(failure_ledger.KGItemFailure).delete()
        s.query(NodeSectionTag).delete()
        s.commit()
    finally:
        s.close()
    yield


def _mk_node(label, node_type="Entity", sentence=None):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type=node_type, original_sentence=sentence))
        s.commit()
    finally:
        s.close()
    return nid


def _mk_edges(n):
    src, tgt = _mk_node("Src"), _mk_node("Tgt")
    s = get_session()
    eids = []
    try:
        for i in range(n):
            eid = str(uuid.uuid4())
            s.add(Edge(id=eid, source_id=src, target_id=tgt, relationship_type=f"rel_{i}"))
            eids.append(eid)
        s.commit()
    finally:
        s.close()
    return eids


def _importance(eids):
    s = get_session()
    try:
        return {e.id: e.importance for e in s.query(Edge).filter(Edge.id.in_(eids)).all()}
    finally:
        s.close()


def _failures(kind):
    s = get_session()
    try:
        return {r.item_id: r.failures for r in
                s.query(failure_ledger.KGItemFailure).filter(failure_ledger.KGItemFailure.item_kind == kind).all()}
    finally:
        s.close()


def _ids_in(task):
    return [line[len("id: "):] for line in task.splitlines() if line.startswith("id: ")]


class _Rater:
    """Fake edge rater: answer(ids, call_no) -> ratings list. Records every call's agent_input."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def action_handler(self, msg):
        self.calls.append(dict(msg.agent_input))
        return SimpleNamespace(data={"ratings": self.answer(_ids_in(msg.agent_input["task"]), len(self.calls))})


def _use(monkeypatch, agent):
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: agent)


def _rate(**kw):
    return scoring.regenerate_edge_importance(only_unrated=True, scope_context=None, **kw)


# ── Edge rater ─────────────────────────────────────────────────────────────


def test_a_zero_score_is_written_as_zero(monkeypatch):
    eids = _mk_edges(2)
    _use(monkeypatch, _Rater(lambda ids, n: [{"id": i, "score": 0.0, "reason": "r"} for i in ids]))
    assert _rate() == 2
    assert set(_importance(eids).values()) == {0.0}


def test_an_unrated_edge_gets_one_correction_for_exactly_it(monkeypatch):
    eids = _mk_edges(3)
    left_out = eids[1]

    def answer(ids, n):
        return [{"id": i, "score": 6.0, "reason": "r"} for i in ids if n > 1 or i != left_out]

    rater = _Rater(answer)
    _use(monkeypatch, rater)
    assert _rate() == 3
    assert len(rater.calls) == 2
    assert rater.calls[0]["correction_ids"] == []
    assert rater.calls[1]["correction_ids"] == [left_out]
    assert _ids_in(rater.calls[1]["task"]) == [left_out]
    assert set(_importance(eids).values()) == {6.0}
    assert _failures(scoring.EDGE_IMPORTANCE_KIND) == {}


def test_a_non_numeric_score_is_corrected_not_defaulted(monkeypatch):
    eids = _mk_edges(2)

    def answer(ids, n):
        return [{"id": i, "score": "high" if (n == 1 and i == eids[0]) else 3.0, "reason": "r"} for i in ids]

    rater = _Rater(answer)
    _use(monkeypatch, rater)
    _rate()
    assert rater.calls[1]["correction_ids"] == [eids[0]]
    assert set(_importance(eids).values()) == {3.0}


def test_still_unrated_after_the_correction_raises_and_writes_nothing(monkeypatch):
    eids = _mk_edges(3)
    left_out = eids[2]
    rater = _Rater(lambda ids, n: [{"id": i, "score": 7.0, "reason": "r"} for i in ids if i != left_out])
    _use(monkeypatch, rater)
    with pytest.raises(ValueError, match=left_out):
        _rate()
    assert len(rater.calls) == 2, "one correction, no more"
    assert set(_importance(eids).values()) == {None}, "nothing of the batch is written"
    assert _failures(scoring.EDGE_IMPORTANCE_KIND) == {left_out: 1}, "counted against the unrated edge only"


def test_a_failed_call_stops_the_run_before_later_batches(monkeypatch):
    eids = _mk_edges(3)
    calls = []

    def boom(msg):
        calls.append(msg)
        raise RuntimeError("Connection error.")

    _use(monkeypatch, SimpleNamespace(action_handler=boom))
    with pytest.raises(RuntimeError, match="Connection error"):
        _rate(batch_size=2)
    assert len(calls) == 1, "the run stops at the first failed batch"
    assert set(_importance(eids).values()) == {None}
    assert set(_failures(scoring.EDGE_IMPORTANCE_KIND)) == set(_ids_in(calls[0].agent_input["task"]))


def test_an_unavailable_rater_raises(monkeypatch):
    _mk_edges(1)
    _use(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unavailable"):
        _rate()


def test_a_set_aside_edge_is_skipped_and_a_success_clears_the_count(monkeypatch):
    eids = _mk_edges(2)
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(scoring.EDGE_IMPORTANCE_KIND, eids[0], RuntimeError("x"))
    failure_ledger.record_failure(scoring.EDGE_IMPORTANCE_KIND, eids[1], RuntimeError("x"))
    rater = _Rater(lambda ids, n: [{"id": i, "score": 4.0, "reason": "r"} for i in ids])
    _use(monkeypatch, rater)
    assert _rate() == 1
    assert _ids_in(rater.calls[0]["task"]) == [eids[1]]
    assert _importance(eids) == {eids[0]: None, eids[1]: 4.0}
    assert set(_failures(scoring.EDGE_IMPORTANCE_KIND)) == {eids[0]}


class _FailingWrites:
    """A db manager whose reads are real and whose write transactions fail."""

    def __init__(self, real):
        self.real = real

    def read_session(self):
        return self.real.read_session()

    @contextmanager
    def transaction(self, op=None):
        def query(*a, **k):
            raise RuntimeError("database is locked")
        yield SimpleNamespace(query=query, commit=lambda: None)


@pytest.mark.parametrize("derive", [scoring.regenerate_entity_importance, scoring.regenerate_state_importance])
def test_a_failed_derivation_write_raises(monkeypatch, derive):
    ent = _mk_node("Person")
    st = _mk_node("Lives somewhere", node_type="State", sentence="The person lives somewhere.")
    s = get_session()
    try:
        s.add(Edge(id=str(uuid.uuid4()), source_id=ent, target_id=st, relationship_type="has_state",
                   importance=8.0))
        s.query(Node).filter(Node.id == ent).update({Node.importance: 8.0})
        s.commit()
    finally:
        s.close()
    failing = _FailingWrites(scoring.get_db_manager())
    monkeypatch.setattr(scoring, "get_db_manager", lambda: failing)
    with pytest.raises(RuntimeError, match="database is locked"):
        derive(only_unrated=False)


# ── Section tagger ─────────────────────────────────────────────────────────


class _Tagger:
    """Fake tagger: answer(numbers, call_no) -> results list. Records every call's agent_input."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def action_handler(self, msg):
        self.calls.append(dict(msg.agent_input))
        numbers = [int(line[1:line.index("]")]) for line in msg.agent_input["nodes_block"].splitlines()
                   if line.startswith("[")]
        return SimpleNamespace(data={"results": self.answer(numbers, len(self.calls)), "reason": "r"})


def _tagged(number, card=("notes",)):
    return {"number": number, "tags": [{"namespace": "card", "sections": list(card)},
                                       {"namespace": "wiki", "sections": []}]}


def _mk_states(n):
    return [_mk_node(f"State {i}", node_type="State", sentence=f"Fact number {i}.") for i in range(n)]


def _tag_rows(node_ids):
    s = get_session()
    try:
        return {(r.node_id, r.namespace, r.section_name)
                for r in s.query(NodeSectionTag).filter(NodeSectionTag.node_id.in_(node_ids)).all()}
    finally:
        s.close()


def test_an_omitted_node_gets_one_correction_under_its_number(monkeypatch):
    nids = _mk_states(3)
    tagger = _Tagger(lambda nums, n: [_tagged(i) for i in nums if n > 1 or i != 2])
    _use(monkeypatch, tagger)
    stats = section_tagging.backfill_untagged_nodes(scope_context=None)
    assert len(tagger.calls) == 2
    assert tagger.calls[0]["correction_numbers"] == []
    assert tagger.calls[1]["correction_numbers"] == [2]
    assert tagger.calls[1]["nodes_block"].startswith("[2] ")
    assert stats["tagged"] == 3 and stats["marked_processed"] == 0
    assert _tag_rows(nids) == {(nid, "card", "notes") for nid in nids}


def test_still_omitted_raises_and_marks_nothing_processed(monkeypatch):
    nids = _mk_states(3)
    tagger = _Tagger(lambda nums, n: [_tagged(i) for i in nums if i != 3])
    _use(monkeypatch, tagger)
    with pytest.raises(ValueError, match="after one correction"):
        section_tagging.backfill_untagged_nodes(scope_context=None)
    assert len(tagger.calls) == 2
    assert _tag_rows(nids) == set(), "nothing of the batch is written, and no sentinel for the omitted node"
    assert len(_failures(section_tagging.SECTION_TAG_KIND)) == 1


def test_an_answered_reject_is_still_marked_processed(monkeypatch):
    [nid] = _mk_states(1)
    _use(monkeypatch, _Tagger(lambda nums, n: [_tagged(i, card=()) for i in nums]))
    stats = section_tagging.backfill_untagged_nodes(scope_context=None)
    assert stats["marked_processed"] == 1
    assert _tag_rows([nid]) == {(nid, section_tagging.NAMESPACE_PROCESSED, section_tagging.PROCESSED_SENTINEL)}


def test_a_failed_tagger_call_stops_the_run(monkeypatch):
    nids = _mk_states(2)

    def boom(msg):
        raise RuntimeError("Connection error.")

    _use(monkeypatch, SimpleNamespace(action_handler=boom))
    with pytest.raises(RuntimeError, match="Connection error"):
        section_tagging.backfill_untagged_nodes(scope_context=None)
    assert _tag_rows(nids) == set()
    assert set(_failures(section_tagging.SECTION_TAG_KIND)) == set(nids)


def test_a_failed_tag_write_raises(monkeypatch):
    nids = _mk_states(2)
    _use(monkeypatch, _Tagger(lambda nums, n: [_tagged(i) for i in nums]))

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(section_tagging, "replace_tags", broken)
    with pytest.raises(RuntimeError, match="database is locked"):
        section_tagging.backfill_untagged_nodes(scope_context=None)
    assert set(_failures(section_tagging.SECTION_TAG_KIND)) == set(nids)


def test_an_unavailable_tagger_raises(monkeypatch):
    _mk_states(1)
    _use(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unavailable"):
        section_tagging.backfill_untagged_nodes(scope_context=None)


def test_backfill_skips_set_aside_nodes(monkeypatch):
    nids = _mk_states(2)
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure(section_tagging.SECTION_TAG_KIND, nids[0], RuntimeError("x"))
    tagger = _Tagger(lambda nums, n: [_tagged(i) for i in nums])
    _use(monkeypatch, tagger)
    stats = section_tagging.backfill_untagged_nodes(scope_context=None)
    assert stats["to_tag"] == 1
    assert _tag_rows(nids) == {(nids[1], "card", "notes")}


# ── The routine ────────────────────────────────────────────────────────────


def _routine_with(monkeypatch, *, rate, tag):
    from app.assistant.kg import section_tagging as st
    from app.assistant.scope import loader
    ran = []
    monkeypatch.setattr(loader, "load_scope_for_source", lambda **kw: None)
    monkeypatch.setattr(scoring, "regenerate_edge_importance", lambda **kw: (ran.append("rate"), rate())[1])
    monkeypatch.setattr(scoring, "regenerate_entity_importance", lambda **kw: ran.append("entity") or 0)
    monkeypatch.setattr(scoring, "regenerate_state_importance", lambda **kw: ran.append("state") or 0)
    monkeypatch.setattr(st, "backfill_untagged_nodes", lambda **kw: (ran.append("tag"), tag())[1])
    return ran


def _raise(exc):
    def f():
        raise exc
    return f


def test_the_routine_stops_at_a_failed_rating(monkeypatch):
    from app.assistant.routine_manager.routine_functions import _lazy_kg_importance_rater
    ran = _routine_with(monkeypatch, rate=_raise(RuntimeError("rater down")), tag=lambda: {})
    with pytest.raises(RuntimeError, match="rater down"):
        _lazy_kg_importance_rater()
    assert ran == ["rate"], "no derivation or tagging after a failed rating"


def test_the_routine_lets_a_tagging_failure_escape(monkeypatch):
    from app.assistant.routine_manager.routine_functions import _lazy_kg_importance_rater
    ran = _routine_with(monkeypatch, rate=lambda: 0, tag=_raise(ValueError("tagger incomplete")))
    with pytest.raises(ValueError, match="tagger incomplete"):
        _lazy_kg_importance_rater()
    assert ran == ["rate", "entity", "state", "tag"]
