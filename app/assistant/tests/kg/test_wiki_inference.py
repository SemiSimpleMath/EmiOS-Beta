"""End-to-end tests for the wiki_connection_investigator pipeline step.

Architecture under test (2026-05-07 redesign — slow-down version):

  - The agent produces synthetic SENTENCES (not edge structures) plus
    suggested dates extracted from the page.
  - The step writes each sentence as a kg_maintenance_finding with
    finding_type='wiki_inferred_fact'. The user reviews, fills any
    missing dates, and explicitly approves via the maintenance UI
    BEFORE any ingestion or KG mutation. NO auto-ingestion.
  - Dedup: the same canonical sentence on the same subject doesn't
    surface twice (text-level hash check on top of upsert_finding's
    pair-key dedup).

The agent itself is stubbed.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_maintenance_pipeline.step_wiki_inference import (
    FINDING_TYPE,
    _canonical_sentence_hash,
    _format_subject_neighborhood,
    _wiki_finding_already_exists,
    run as run_wiki_inference,
)
from app.models.base import Base, get_session


JUKKA_ID = "11111111-jukk-jukk-jukk-111111111111"
DIANA_ID = "22222222-dian-dian-dian-222222222222"


@pytest.fixture
def vault_dir():
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        (vault / "prose").mkdir(parents=True)
        prev = os.environ.get("EMI_WIKI_DIR")
        os.environ["EMI_WIKI_DIR"] = str(vault)
        try:
            yield vault
        finally:
            if prev is None:
                os.environ.pop("EMI_WIKI_DIR", None)
            else:
                os.environ["EMI_WIKI_DIR"] = prev


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
        session.add(Node(
            id=JUKKA_ID, label="Jukka", node_type="Person",
            description="The user", importance=9.5,
        ))
        session.add(Node(
            id=DIANA_ID, label="Diana", node_type="Person",
            description="Jukka's sister", importance=7.0,
        ))
        session.add(Edge(
            id="aaaaaaaa-aaaa-aaaa-aaaa-edge0000aaaa",
            source_id=DIANA_ID, target_id=JUKKA_ID,
            relationship_type="sister_of", sentence="Diana is Jukka's sister.",
        ))
        session.commit()
    finally:
        session.close()


class StubAgentResult:
    def __init__(self, data: dict):
        self.data = data


class StubAgent:
    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.calls = []

    def action_handler(self, message):
        self.calls.append(message)
        return StubAgentResult(self.verdict)


def _patch_agent(verdict: dict):
    return patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        return_value=StubAgent(verdict),
    )


def _ctx() -> PipelineContext:
    return PipelineContext.for_date(pipeline_id="test_wiki_inference")


def _write_page(vault: Path, label: str, body: str) -> Path:
    p = vault / "prose" / f"{label}.md"
    p.write_text(body, encoding="utf-8")
    return p


def _findings() -> list[KGMaintenanceFinding]:
    session = get_session()
    try:
        return list(
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == FINDING_TYPE)
            .order_by(KGMaintenanceFinding.created_at.asc())
            .all()
        )
    finally:
        session.close()


# ── Tests ─────────────────────────────────────────────────────────────


def test_no_pages_means_no_subjects(vault_dir):
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("agent must not be created when no pages"),
    ):
        result = run_wiki_inference(_ctx())
    assert result["subjects_examined"] == 0
    assert result["findings_written"] == 0


def test_pick_only_pages_with_known_kg_node(vault_dir):
    _write_page(vault_dir, "RandomStranger", "x" * 500)
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("no candidate, no agent"),
    ):
        result = run_wiki_inference(_ctx())
    assert result["subjects_examined"] == 0


def test_writes_finding_for_dylan_kinship_inference(vault_dir):
    """Happy path: page implies 'Dylan is Jukka's nephew.' Step writes a
    wiki_inferred_fact finding — NOT a unified_log row, NOT a claim_proposal.
    The user reviews via the maintenance UI before any ingestion."""
    page_text = (
        "# Jukka\n\n"
        "Jukka has one sister, Diana, who has a son named Dylan. "
        "Dylan visits often.\n\n" + "Filler. " * 60
    )
    _write_page(vault_dir, "Jukka", page_text)
    verdict = {
        "sentences": [{
            "sentence": "Dylan is Jukka's nephew.",
            "evidence_quote": "Diana, who has a son named Dylan",
            "inference_path": "Page: Diana's son → KG: Diana sister_of Jukka → Dylan nephew_of Jukka.",
            "confidence": 0.92,
            "not_already_in_kg": True,
        }],
        "reason": "One new kinship fact.",
    }
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())

    assert result["subjects_examined"] == 1
    assert result["findings_written"] == 1
    assert result["skipped_low_confidence"] == 0
    assert result["skipped_already_in_kg"] == 0
    assert result["skipped_duplicate_finding"] == 0

    findings = _findings()
    assert len(findings) == 1
    f = findings[0]
    assert f.finding_type == FINDING_TYPE
    assert f.status == "pending"
    assert f.primary_node_id == JUKKA_ID
    assert f.suggested_action == "review"
    assert f.agent_name == "wiki_connection_investigator"
    ev = f.evidence_json or {}
    assert ev.get("sentence") == "Dylan is Jukka's nephew."
    assert ev.get("agent_confidence") == 0.92
    assert "Diana" in (ev.get("evidence_quote") or "")
    # No dates given by the agent → null in evidence
    assert ev.get("suggested_start_date") is None
    assert ev.get("suggested_end_date") is None


def test_finding_carries_suggested_dates_from_agent(vault_dir):
    """When the page provides dates inline, the agent's suggested_start_date
    / suggested_end_date / prose fields land on the finding so the user
    can review them at approval time."""
    page_text = "# Diana\n\n" + "Diana taught at UC Berkeley starting in 2018, until early 2024. " * 30
    _write_page(vault_dir, "Diana", page_text)
    verdict = {
        "sentences": [{
            "sentence": "Diana taught at UC Berkeley from 2018 to early 2024.",
            "evidence_quote": "Diana taught at UC Berkeley starting in 2018, until early 2024",
            "inference_path": "Direct from page.",
            "confidence": 0.95,
            "not_already_in_kg": True,
            "suggested_start_date": "2018-01-01",
            "suggested_end_date": "2024-01-01",
            "suggested_start_date_prose": None,
            "suggested_end_date_prose": "early 2024",
        }],
        "reason": "One state with start+end.",
    }
    with _patch_agent(verdict):
        run_wiki_inference(_ctx())

    findings = _findings()
    assert len(findings) == 1
    ev = findings[0].evidence_json
    assert ev["suggested_start_date"] == "2018-01-01"
    assert ev["suggested_end_date"] == "2024-01-01"
    assert ev["suggested_end_date_prose"] == "early 2024"


def test_low_confidence_sentences_skipped(vault_dir):
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)
    verdict = {
        "sentences": [{
            "sentence": "Jukka may know MaybeColleague through work.",
            "evidence_quote": "(speculative)",
            "inference_path": "guess",
            "confidence": 0.4,
            "not_already_in_kg": True,
        }],
        "reason": "speculative",
    }
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())
    assert result["findings_written"] == 0
    assert result["skipped_low_confidence"] == 1
    assert _findings() == []


def test_already_in_kg_sentences_skipped(vault_dir):
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)
    verdict = {
        "sentences": [{
            "sentence": "Jukka and Diana are siblings.",
            "evidence_quote": "his sister Diana",
            "inference_path": "Page says sister.",
            "confidence": 0.95,
            "not_already_in_kg": False,
        }],
        "reason": "(already covered)",
    }
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())
    assert result["findings_written"] == 0
    assert result["skipped_already_in_kg"] == 1
    assert _findings() == []


def test_duplicate_sentence_not_re_surfaced(vault_dir):
    """The same canonical sentence on the same subject shouldn't produce
    two findings across runs. Critical: prevents the nightly job from
    re-flooding the queue with the same inferences when the user hasn't
    yet reviewed them."""
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)
    verdict = {
        "sentences": [{
            "sentence": "Dylan is Jukka's nephew.",
            "evidence_quote": "x",
            "inference_path": "y",
            "confidence": 0.9,
            "not_already_in_kg": True,
        }],
        "reason": "x",
    }

    # First run: creates the finding
    with _patch_agent(verdict):
        first = run_wiki_inference(_ctx())
    assert first["findings_written"] == 1

    # Force a second run by clearing the sidecar (simulating the page
    # being rewritten). Same agent verdict — the dedup should catch it.
    page = vault_dir / "prose" / "Jukka.md"
    sidecar = page.with_suffix(".wiki_inference.json")
    sidecar.unlink()

    with _patch_agent(verdict):
        second = run_wiki_inference(_ctx())
    assert second["findings_written"] == 0
    assert second["skipped_duplicate_finding"] == 1

    # Still only one finding.
    assert len(_findings()) == 1


def test_canonical_sentence_hash_collapses_punctuation_and_case():
    h1 = _canonical_sentence_hash("Dylan is Jukka's nephew.")
    h2 = _canonical_sentence_hash("dylan is jukka's nephew")
    h3 = _canonical_sentence_hash("DYLAN IS JUKKA'S NEPHEW.")
    h4 = _canonical_sentence_hash("Dylan  is  Jukka's nephew")  # collapsed whitespace
    assert h1 == h2 == h3 == h4
    # Different sentence → different hash
    h5 = _canonical_sentence_hash("Dylan is Jukka's son.")
    assert h5 != h1


def test_wiki_finding_already_exists_returns_true_after_write():
    """Direct test of the dedup helper."""
    from app.assistant.kg_maintenance.store import upsert_finding
    sentence = "Dylan is Jukka's nephew."
    h = _canonical_sentence_hash(sentence)
    upsert_finding(
        finding_type=FINDING_TYPE,
        primary_node_id=JUKKA_ID,
        suggested_action="review",
        reason="seed",
        confidence=0.9,
        priority="low",
        agent_name="test",
        evidence={"sentence_hash": h, "sentence": sentence},
    )
    assert _wiki_finding_already_exists(primary_node_id=JUKKA_ID, sentence_text=sentence) is True
    # Different subject — not a dup
    assert _wiki_finding_already_exists(primary_node_id=DIANA_ID, sentence_text=sentence) is False
    # Different sentence — not a dup
    assert _wiki_finding_already_exists(primary_node_id=JUKKA_ID, sentence_text="Other fact.") is False


def test_short_page_skipped(vault_dir):
    _write_page(vault_dir, "Jukka", "Jukka.")
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent"
    ) as mock_create:
        mock_create.return_value = StubAgent({"sentences": [], "reason": "x"})
        result = run_wiki_inference(_ctx())
    assert result["findings_written"] == 0
    assert _findings() == []


def test_subject_neighborhood_includes_existing_edge():
    block = _format_subject_neighborhood(JUKKA_ID)
    assert "Diana" in block
    assert "sister_of" in block


def test_sidecar_blocks_re_examination(vault_dir):
    page = _write_page(vault_dir, "Jukka", "Jukka. " * 200)
    verdict = {"sentences": [], "reason": "nothing new"}
    with _patch_agent(verdict):
        first = run_wiki_inference(_ctx())
    assert first["subjects_examined"] == 1
    sidecar = page.with_suffix(".wiki_inference.json")
    assert sidecar.exists()

    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("agent must not run when nothing fresh"),
    ):
        second = run_wiki_inference(_ctx())
    assert second["subjects_examined"] == 0


def test_subject_limit_kwarg_caps_examined_pages(vault_dir):
    _write_page(vault_dir, "Jukka", "Jukka content. " * 50)
    _write_page(vault_dir, "Diana", "Diana content. " * 50)
    verdict = {"sentences": [], "reason": "x"}
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx(), subject_limit=1)
    assert result["subjects_examined"] == 1
