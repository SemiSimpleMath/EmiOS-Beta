"""Entity cards: a failed call stops the run (owner, 2026-10-05).

Pins: a failed or missing agent raises out of build_card before anything is written (the edge_filter prefilter,
the section distiller, the summary writer); a well-formed but incomplete answer is refused by the agent's validate.py
(the agent corrects once, then raises; the rules are pinned here against each validate.py);
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


def _refused(name):
    from app.assistant.agent_runtime.correction import AgentOutputInvalid, Problem
    return AgentOutputInvalid(name, [Problem(why="the summary is empty", field="summary")])


def test_a_summary_still_refused_after_the_correction_fails_the_build_and_writes_nothing(agents):
    eid = _entity(description="Wiki text that used to become level_0.")
    fake = agents(entity_cards__section_tagger=[_tag_all(2)], entity_cards__bullet_renderer=[_render_each(2)],
                  entity_cards__summary_writer=[_refused("entity_cards::summary_writer")])
    with pytest.raises(ValueError, match="still invalid after one correction"):
        builder.build_card(eid)
    assert len(fake.inputs["entity_cards::summary_writer"]) == 1
    assert _card(eid) is None


def test_each_card_agent_gets_the_ids_its_rules_read_and_no_correction_slot(agents):
    eid = _entity()
    fake = agents(entity_cards__section_tagger=[_tag_all(2)], entity_cards__bullet_renderer=[_render_each(2)],
                  entity_cards__summary_writer=[{"summary": "A friend."}])
    builder.build_card(eid)
    assert _card(eid)["sections"]["summary"] == "A friend."
    [tagger] = fake.inputs["entity_cards::section_tagger"]
    [renderer] = fake.inputs["entity_cards::bullet_renderer"]
    assert tagger["fact_ids"] == ["f1", "f2"] and sorted(renderer["fact_ids"]) == ["f1", "f2"]
    assert not any("correction" in x for xs in fake.inputs.values() for x in xs)


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


def _rules(*parts):
    from app.assistant.agent_registry.agent_registry import AgentRegistry
    from app.assistant.utils.path_utils import get_app_root
    return AgentRegistry()._load_validator(get_app_root().joinpath("assistant", "agents", *parts, "validate.py"))


def test_the_distiller_is_given_its_cap_and_bullet_ids():
    """The distiller's rules moved to its validate.py (2026-11-15); the agent corrects once and raises."""
    rules = _rules("entity_cards", "section_distiller")
    given = {"max_bullets": "2", "bullet_ids": ["b1", "b2", "b3"]}
    over = {"bullets": [{"bullet_text": f"B{i}", "source_bullet_ids": [f"b{i}"]} for i in range(1, 4)]}
    assert [p.why for p in rules.validate(over, given)] == ["3 bullets returned, more than max_bullets 2"]
    assert [p.field for p in rules.validate({"bullets": []}, given)] == ["bullets"]
    bad = {"bullets": [{"bullet_text": "B1", "source_bullet_ids": ["b9"]}, {"bullet_text": " ",
                                                                            "source_bullet_ids": ["b1"]}]}
    assert [(p.at, p.value) for p in rules.validate(bad, given)] == [("[0].source_bullet_ids", ["b9"]),
                                                                      ("[1].bullet_text", None)]
    assert rules.validate({"bullets": [{"bullet_text": "B1", "source_bullet_ids": ["b1", "b2"]}]}, given) == []


def test_a_distiller_call_sends_its_cap_and_bullet_ids(agents):
    entity = Node(id="e", label="Pat", node_type="Entity", category="person")
    rbs = [{"bullet_id": f"b{i}", "bullet_text": f"B{i}"} for i in range(1, 4)]
    fake = agents(entity_cards__section_distiller=[{"bullets": [{"bullet_text": "B1", "source_bullet_ids": ["b1"]}]}])
    assert builder._run_section_distiller(entity, "notes", rbs, 2) == [
        {"bullet_text": "B1", "source_bullet_ids": ["b1"]}]
    [sent] = fake.inputs["entity_cards::section_distiller"]
    assert sent["max_bullets"] == "2" and sent["bullet_ids"] == ["b1", "b2", "b3"]


def test_the_tagger_renderer_and_summary_rules():
    tagger = _rules("entity_cards", "section_tagger")
    one = {"tagged_facts": [{"fact_id": "f1", "section": "notes", "confidence": "high", "reason": "x"}]}
    assert [(p.value, p.structural) for p in tagger.validate(one, {"fact_ids": ["f1", "f2"]})] == [("f2", True)]
    renderer = _rules("entity_cards", "bullet_renderer")
    bad = {"bullets": [{"bullet_text": "B", "source_fact_ids": []}, {"bullet_text": "C", "source_fact_ids": ["f7"]}]}
    assert [p.at for p in renderer.validate(bad, {"fact_ids": ["f1"]})] == ["[0].source_fact_ids",
                                                                           "[1].source_fact_ids"]
    assert renderer.validate({"bullets": []}, {"fact_ids": ["f1"]}) == []   # dropping a fact is its call
    summary = _rules("entity_cards", "summary_writer")
    assert [p.field for p in summary.validate({"summary": "  "}, {})] == ["summary"]
    assert summary.validate({"summary": "A friend."}, {}) == []


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


def test_a_prefilter_batch_is_asked_once_with_its_line_count(agents):
    fake = agents(kg_maintenance__edge_filter=[{"keep_indices": [1, 3]}])
    facts = _facts(61)
    assert builder._prefilter_facts(facts, "Pat", "Entity") == [facts[0], facts[2]]
    [sent] = fake.inputs["kg_maintenance::edge_filter"]
    assert sent["line_count"] == 61 and "correction" not in sent


def test_the_edge_filter_rules():
    """Moved from builder._keep_indices_problems to the filter's validate.py (2026-11-15)."""
    rules = _rules("kg_maintenance", "edge_filter")
    assert [p.why for p in rules.validate({"keep_indices": []}, {"line_count": 5})] == [
        "keep_indices is empty: list the numbers of the edges to keep (1 to 5)"]
    assert [p.value for p in rules.validate({"keep_indices": [0, 2, 99]}, {"line_count": 5})] == [[0, 99]]
    assert rules.validate({"keep_indices": [1, 5]}, {"line_count": 5}) == []


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
