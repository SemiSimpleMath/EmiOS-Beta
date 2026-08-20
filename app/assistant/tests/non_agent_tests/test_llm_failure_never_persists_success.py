"""An LLM failure must never persist as success (2026-08-20 audit, five sites).

The audit before merging the quota-breaker PR found five writers that caught an
LLM failure broadly and then wrote state recording success: entity cards gutted
and stamped freshly built, KG enrichment rows persisted empty (removing the
extraction from the pending pool forever), wiki tag sidecars poisoned with []
verdicts, growth's "not biographical" skip-forever marker minted from an outage,
and the incremental refresh republishing stale prose over advanced watermarks
while dropping the lead. All five now RAISE; these tests pin the raising seams.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401


class _RaisingAgent:
    def action_handler(self, msg):
        raise RuntimeError("simulated LLM outage")


class _ScriptedAgent:
    def __init__(self, data):
        self._data = data

    def action_handler(self, msg):
        return SimpleNamespace(data=self._data)


# ── C3: wiki tag cache ────────────────────────────────────────────


class TestTagger:

    def _kwargs(self, bullets):
        return dict(
            entity_label="TestEntity",
            entity_type="Person",
            bullets=bullets,
            allowed_sections_block="- life",
            allowed_keys=["life"],
            cached_tags={},
        )

    def test_failed_batch_raises_instead_of_minting_empty_verdicts(self):
        from app.assistant.kg_projection.tagger import tag_bullets
        with pytest.raises(RuntimeError, match="simulated LLM outage"):
            tag_bullets(_RaisingAgent(), **self._kwargs(["- fact one"]))

    def test_omitted_bullet_is_left_uncached_not_judged(self):
        from app.assistant.kg_projection import tagger
        agent = _ScriptedAgent({"results": [{"number": 1, "sections": ["life"]}]})
        bullets = ["- fact one", "- fact two"]
        tags = tagger.tag_bullets(agent, **self._kwargs(bullets))
        k1, k2 = tagger.bullet_key(bullets[0]), tagger.bullet_key(bullets[1])
        assert tags[k1] == ["life"]
        assert k2 not in tags          # no verdict != "belongs nowhere"


# ── C2: KG enrichment ─────────────────────────────────────────────


class TestEnrichExtraction:

    def _step_and_nodes(self):
        from app.assistant.pipelines.kg_pipeline.steps.enrich_extraction import (
            EnrichExtractionStep,
        )
        nodes = [{"temp_id": "t1", "node_type": "State", "label": "x",
                  "category": "c", "sentence": "s"}]
        return EnrichExtractionStep(), nodes

    def test_agent_call_failure_raises(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        step, nodes = self._step_and_nodes()
        monkeypatch.setattr(
            DI, "agent_factory",
            SimpleNamespace(create_agent=lambda name: _RaisingAgent()),
            raising=False,
        )
        with pytest.raises(RuntimeError, match="simulated LLM outage"):
            step._call_meta_data_add(nodes, [], "window text", "2026-08-20")

    def test_missing_agent_raises_not_degrades(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        step, nodes = self._step_and_nodes()
        monkeypatch.setattr(
            DI, "agent_factory",
            SimpleNamespace(create_agent=lambda name: None),
            raising=False,
        )
        with pytest.raises(RuntimeError, match="could not create agent"):
            step._call_meta_data_add(nodes, [], "window text", "2026-08-20")


# ── C1: entity card summary ───────────────────────────────────────


class TestCardSummary:

    def test_summary_writer_failure_raises_instead_of_none(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.pipelines.entity_cards_v2 import builder
        monkeypatch.setattr(
            DI, "agent_factory",
            SimpleNamespace(create_agent=lambda name: _RaisingAgent()),
            raising=False,
        )
        entity = SimpleNamespace(label="TestEntity", node_type="Entity", category="person")
        with pytest.raises(RuntimeError, match="simulated LLM outage"):
            builder._build_summary_section(entity, {"life": [{"bullet_text": "b"}]}, None)


# ── C5 (lead half): wiki lead writer ──────────────────────────────


class TestLeadWriter:

    def test_lead_failure_raises_instead_of_empty_string(self, monkeypatch):
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.wiki_generator import lead_writer
        monkeypatch.setattr(
            DI, "agent_factory",
            SimpleNamespace(create_agent=lambda name: _RaisingAgent()),
            raising=False,
        )
        with pytest.raises(RuntimeError, match="simulated LLM outage"):
            lead_writer.generate_lead(
                entity_name="TestEntity", entity_type="Person",
                article_body="## Life\n\nSome text.",
            )

    def test_no_body_still_returns_empty(self):
        from app.assistant.wiki_generator import lead_writer
        assert lead_writer.generate_lead(
            entity_name="TestEntity", entity_type="Person", article_body="",
        ) == ""
