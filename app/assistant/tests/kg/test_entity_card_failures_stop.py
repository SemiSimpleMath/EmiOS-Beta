"""Entity cards: a failed call stops the run (owner, 2026-10-05).

Pins: a failed or missing agent raises out of build_card before anything is written (the edge_filter prefilter,
the section distiller, the summary writer); a well-formed but incomplete answer gets one correction, then raises;
level_0 comes only from the summary. The nightly refresh stops at the first failed build and records it on the
failure ledger; a set-aside entity is skipped; a new card is built inactive and activated only when the critic
passes it, stays inactive on a veto, and is discarded (so the next run rebuilds and judges it) when the critic call
fails.

Uses the kg conftest (isolated test DB, fresh tables per test). Fake agents, invented data only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.entity_management.entity_card_v2 import EntityCardSection, EntityCardV2
from app.assistant.kg import failure_ledger
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node, NodeSectionTag
from app.assistant.pipelines.entity_cards_v2 import builder, refresh_subscriber
from app.models.base import get_session

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("PRIMARY_USER", "Sam")
    s = get_session()
    try:
        failure_ledger._ensure(s)
        for tbl in (EntityCardV2, NodeSectionTag, failure_ledger.KGItemFailure):
            s.query(tbl).delete()
        s.commit()
    finally:
        s.close()
    yield


class Agents:
    """Fake agent factory: name -> list of answers (data dicts, or exceptions to raise), one per call."""

    def __init__(self, **answers):
        self.answers = {k.replace("__", "::"): list(v) for k, v in answers.items()}
        self.inputs = {}

    def create_agent(self, name):
        if name not in self.answers:
            return None
        factory = self

        class _Agent:
            def action_handler(self, msg):
                factory.inputs.setdefault(name, []).append(dict(msg.agent_input))
                answer = factory.answers[name].pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return SimpleNamespace(data=answer)
        return _Agent()


@pytest.fixture
def agents(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI

    def install(**answers):
        fake = Agents(**answers)
        monkeypatch.setattr(DI, "agent_factory", fake, raising=False)
        return fake
    return install


def _entity(label="Pat", n_facts=2, **fields):
    eid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=eid, label=label, node_type="Entity", category="person",
                   locked_by_user_at=fields.pop("locked_by_user_at", OLD), **fields))
        for i in range(n_facts):
            nid = str(uuid.uuid4())
            s.add(Node(id=nid, label=f"Fact {i}", node_type="State", original_sentence=f"{label} fact {i}."))
            s.add(Edge(id=str(uuid.uuid4()), source_id=eid, target_id=nid, relationship_type="has_state",
                       sentence=f"{label} has fact {i}"))
        s.commit()
    finally:
        s.close()
    return eid


def _card(eid):
    s = get_session()
    try:
        card = s.query(EntityCardV2).filter(EntityCardV2.entity_node_id == eid).first()
        if card is None:
            return None
        sections = {x.section_name: x.intro_text for x in
                    s.query(EntityCardSection).filter(EntityCardSection.card_id == card.id).all()}
        return {"is_active": card.is_active, "sections": sections}
    finally:
        s.close()


def _tag_all(n):
    return {"tagged_facts": [{"fact_id": f"f{i}", "section": "general_facts", "confidence": "high", "reason": "x"}
                             for i in range(1, n + 1)]}


def _render_each(n):
    return {"bullets": [{"bullet_text": f"Bullet {i}.", "source_fact_ids": [f"f{i}"]} for i in range(1, n + 1)]}


# ── build_card ────────────────────────────────────────────────────────────


def test_a_built_card_takes_level_0_from_its_summary_and_can_be_written_inactive(agents):
    eid = _entity()
    agents(entity_cards__section_tagger=[_tag_all(2)], entity_cards__bullet_renderer=[_render_each(2)],
           entity_cards__summary_writer=[{"summary": "A friend of the owner. Lives nearby."}])
    assert builder.build_card(eid, activate_new=False)["status"] == "ok"
    card = _card(eid)
    assert card["is_active"] is False
    assert card["sections"]["level_0"] == "A friend of the owner."


def test_an_empty_summary_gets_one_correction_then_the_build_raises_and_writes_nothing(agents):
    eid = _entity(description="Wiki text that used to become level_0.")
    fake = agents(entity_cards__section_tagger=[_tag_all(2)], entity_cards__bullet_renderer=[_render_each(2)],
                  entity_cards__summary_writer=[{"summary": ""}, {"summary": "  "}])
    with pytest.raises(RuntimeError, match="still incomplete after one correction"):
        builder.build_card(eid)
    sent = fake.inputs["entity_cards::summary_writer"]
    assert [x["correction"] for x in sent] == ["", "the summary is empty: write the 2-3 sentence summary"]
    assert _card(eid) is None


def test_a_summary_corrected_once_is_used(agents):
    eid = _entity()
    agents(entity_cards__section_tagger=[_tag_all(2)], entity_cards__bullet_renderer=[_render_each(2)],
           entity_cards__summary_writer=[{"summary": ""}, {"summary": "A friend."}])
    builder.build_card(eid)
    assert _card(eid)["sections"]["summary"] == "A friend."


def test_a_summary_writer_without_a_structured_answer_raises(agents):
    agents(entity_cards__summary_writer=["not a dict"])
    entity = Node(id="e", label="Pat", node_type="Entity", category="person")
    with pytest.raises(RuntimeError, match="no structured answer"):
        builder._build_summary_section(entity, {"notes": [{"bullet_text": "b"}]}, None)


def test_a_missing_summary_writer_or_nothing_to_summarize_raises(agents):
    agents()
    entity = Node(id="e", label="Pat", node_type="Entity", category="person")
    with pytest.raises(RuntimeError, match="summary_writer not available"):
        builder._build_summary_section(entity, {"notes": [{"bullet_text": "b"}]}, None)
    with pytest.raises(RuntimeError, match="no bullets or aliases"):
        builder._build_summary_section(entity, {}, None)


def test_a_failed_distiller_raises_and_the_existing_card_is_left_as_it_was(agents):
    eid = _entity(n_facts=11)  # general_facts caps at 10, so the distiller runs
    distilled = {"bullets": [{"bullet_text": "Bullet 1.", "source_bullet_ids": ["b1", "b2"]}]}
    agents(entity_cards__section_tagger=[_tag_all(11)], entity_cards__bullet_renderer=[_render_each(11)],
           entity_cards__section_distiller=[distilled], entity_cards__summary_writer=[{"summary": "Old summary."}])
    builder.build_card(eid)
    before = _card(eid)
    agents(entity_cards__section_tagger=[_tag_all(11)], entity_cards__bullet_renderer=[_render_each(11)],
           entity_cards__section_distiller=[RuntimeError("simulated LLM outage")])
    with pytest.raises(RuntimeError, match="simulated LLM outage"):
        builder.build_card(eid)
    assert _card(eid) == before


def test_a_distiller_answer_over_the_cap_gets_one_correction_then_raises(agents):
    entity = Node(id="e", label="Pat", node_type="Entity", category="person")
    rbs = [{"bullet_id": f"b{i}", "bullet_text": f"B{i}"} for i in range(1, 5)]
    over = {"bullets": [{"bullet_text": f"B{i}", "source_bullet_ids": [f"b{i}"]} for i in range(1, 4)]}
    agents(entity_cards__section_distiller=[over, over])
    with pytest.raises(RuntimeError, match="more than max_bullets 2"):
        builder._run_section_distiller(entity, "notes", rbs, 2)


def test_a_distiller_bullet_citing_an_unknown_id_is_corrected(agents):
    entity = Node(id="e", label="Pat", node_type="Entity", category="person")
    rbs = [{"bullet_id": f"b{i}", "bullet_text": f"B{i}"} for i in range(1, 4)]
    bad = {"bullets": [{"bullet_text": "B1", "source_bullet_ids": ["b9"]}]}
    good = {"bullets": [{"bullet_text": "B1", "source_bullet_ids": ["b1"]}]}
    fake = agents(entity_cards__section_distiller=[bad, good])
    assert builder._run_section_distiller(entity, "notes", rbs, 2) == [
        {"bullet_text": "B1", "source_bullet_ids": ["b1"]}]
    assert "b9" in fake.inputs["entity_cards::section_distiller"][1]["correction"]


def test_a_tagger_that_skips_a_fact_is_corrected_once_then_raises(agents):
    entity = Node(id="e", label="Pat", node_type="Entity", category="person", original_sentence="", description="")
    facts = [{"fact_id": f"f{i}", "direction": "outgoing", "edge_type": "has_state", "object_type": "State",
              "object_label": "x", "edge_sentence": "", "object_sentence": "", "confidence_tier": "provisional",
              "temporal": {"start_date": None, "end_date": None, "valid_during": None}} for i in (1, 2)]
    one = {"tagged_facts": [{"fact_id": "f1", "section": "notes", "confidence": "high", "reason": "x"}]}
    agents(entity_cards__section_tagger=[one, one])
    with pytest.raises(RuntimeError, match=r"\['f2'\] have no tagged_facts entry"):
        builder._run_section_tagger(entity, facts, ["notes", "general_facts"])


# ── edge_filter prefilter ─────────────────────────────────────────────────


def _facts(n):
    return [{"fact_id": f"f{i}", "direction": "outgoing", "edge_type": "has_state", "object_label": f"x{i}",
             "object_type": "State", "edge_sentence": f"s{i}"} for i in range(n)]


def test_the_prefilter_raises_when_its_agent_is_missing(agents):
    agents()
    with pytest.raises(RuntimeError, match="edge_filter not available"):
        builder._prefilter_facts(_facts(61), "Pat", "Entity")


def test_a_failed_prefilter_batch_raises_instead_of_keeping_every_fact(agents):
    agents(kg_maintenance__edge_filter=[RuntimeError("simulated LLM outage")])
    with pytest.raises(RuntimeError, match="simulated LLM outage"):
        builder._prefilter_facts(_facts(61), "Pat", "Entity")


def test_a_prefilter_batch_with_no_keep_indices_is_corrected_once(agents):
    fake = agents(kg_maintenance__edge_filter=[{"keep_indices": []}, {"keep_indices": [1, 3]}])
    facts = _facts(61)
    assert builder._prefilter_facts(facts, "Pat", "Entity") == [facts[0], facts[2]]
    sent = fake.inputs["kg_maintenance::edge_filter"]
    assert sent[0]["correction"] == "" and "keep_indices is empty" in sent[1]["correction"]


def test_a_prefilter_batch_still_incomplete_after_one_correction_raises(agents):
    agents(kg_maintenance__edge_filter=[{"keep_indices": []}, {"keep_indices": [0, 99]}])
    with pytest.raises(RuntimeError, match=r"still incomplete after one correction: keep_indices \[0, 99\]"):
        builder._prefilter_facts(_facts(61), "Pat", "Entity")


# ── nightly refresh ───────────────────────────────────────────────────────


def _stale_card(label, importance):
    eid = _entity(label=label, n_facts=0, importance=importance)
    s = get_session()
    try:
        s.add(EntityCardV2(id=str(uuid.uuid4()), entity_node_id=eid, entity_type="Entity", is_active=True,
                           card_title=label, last_built_at=OLD))
        s.commit()
    finally:
        s.close()
    return eid


def _ledger(eid):
    s = get_session()
    try:
        row = s.get(failure_ledger.KGItemFailure, ("entity_card", eid))
        return None if row is None else row.failures
    finally:
        s.close()


def test_the_refresh_stops_at_the_first_failed_rebuild_and_records_it(monkeypatch):
    first = _stale_card("Pat", 9.0)
    _stale_card("Lee", 1.0)
    built = []

    def build(nid, **kw):
        built.append(nid)
        raise RuntimeError("simulated LLM outage")
    monkeypatch.setattr(builder, "build_card", build)
    with pytest.raises(RuntimeError, match="simulated LLM outage"):
        refresh_subscriber.run_card_refresh(max_new=0)
    assert built == [first]
    assert _ledger(first) == 1


def test_a_rebuild_reporting_an_error_status_stops_the_run(monkeypatch):
    eid = _stale_card("Pat", 9.0)
    monkeypatch.setattr(builder, "build_card", lambda nid, **kw: {"status": "error", "reason": "x"})
    with pytest.raises(RuntimeError, match="card rebuild for 'Pat' failed"):
        refresh_subscriber.run_card_refresh(max_new=0)
    assert _ledger(eid) == 1


def test_a_successful_rebuild_clears_the_ledger_and_a_set_aside_entity_is_skipped(monkeypatch):
    ok = _stale_card("Pat", 9.0)
    aside = _stale_card("Lee", 5.0)
    failure_ledger.record_failure("entity_card", ok, RuntimeError("earlier"))
    for _ in range(failure_ledger.SET_ASIDE_AFTER):
        failure_ledger.record_failure("entity_card", aside, RuntimeError("always fails"), label="Lee")
    built = []
    monkeypatch.setattr(builder, "build_card", lambda nid, **kw: built.append(nid) or {"status": "ok"})
    summary = refresh_subscriber.run_card_refresh(max_new=0)
    assert built == [ok] and summary["rebuilt"] == 1 and summary["set_aside"] == 1
    assert _ledger(ok) is None


def _fake_new_build(monkeypatch):
    def build(nid, *, activate_new=True):
        s = get_session()
        try:
            s.add(EntityCardV2(id=str(uuid.uuid4()), entity_node_id=nid, entity_type="Entity",
                               is_active=activate_new, card_title="Pat"))
            s.commit()
        finally:
            s.close()
        return {"status": "ok"}
    monkeypatch.setattr(builder, "build_card", build)


def test_a_new_card_is_activated_only_when_the_critic_passes_it(monkeypatch, agents):
    eid = _entity()
    _fake_new_build(monkeypatch)
    agents(entity_cards__card_critic=[{"verdict": "pass", "reason": "useful"}])
    summary = refresh_subscriber.run_card_refresh()
    assert summary["built_new"] == 1 and _card(eid)["is_active"] is True


def test_a_vetoed_new_card_stays_inactive_and_is_not_offered_again(monkeypatch, agents):
    eid = _entity()
    _fake_new_build(monkeypatch)
    agents(entity_cards__card_critic=[{"verdict": "veto", "reason": "noise"}])
    assert refresh_subscriber.run_card_refresh()["vetoed_new"] == 1
    assert _card(eid)["is_active"] is False
    assert refresh_subscriber.run_card_refresh(dry_run=True)["new_selected"] == []


def test_a_failed_critic_discards_the_unjudged_card_stops_the_run_and_the_next_run_judges_it(monkeypatch, agents):
    eid = _entity()
    _fake_new_build(monkeypatch)
    agents(entity_cards__card_critic=[RuntimeError("simulated LLM outage"), {"verdict": "pass", "reason": "ok"}])
    with pytest.raises(RuntimeError, match="simulated LLM outage"):
        refresh_subscriber.run_card_refresh()
    assert _card(eid) is None and _ledger(eid) == 1
    assert refresh_subscriber.run_card_refresh()["built_new"] == 1
    assert _card(eid)["is_active"] is True and _ledger(eid) is None


def test_a_critic_answer_without_a_verdict_counts_as_a_failure(monkeypatch, agents):
    eid = _entity()
    _fake_new_build(monkeypatch)
    agents(entity_cards__card_critic=[{"reason": "no verdict"}])
    with pytest.raises(RuntimeError, match="card_critic returned no verdict"):
        refresh_subscriber.run_card_refresh()
    assert _card(eid) is None
