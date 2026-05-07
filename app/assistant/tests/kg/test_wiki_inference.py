"""End-to-end tests for the wiki_connection_investigator pipeline step.

The agent itself is stubbed — we test the candidate-page picker, the
already-in-KG verification, and the proposal-write path. The agent's
prompt quality is a separate concern.

Each test covers a piece of self-healing the user asked for: turn a
factual implication on a wiki page into a claim_proposal that the
promoter then merges into the KG.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalEdge, ClaimProposalNode, ClaimProposalEvidence,
)
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_maintenance_pipeline.step_wiki_inference import (
    _edge_exists,
    _format_subject_neighborhood,
    run as run_wiki_inference,
)
from app.models.base import get_session


# Stable test ids
JUKKA_ID = "11111111-jukk-jukk-jukk-111111111111"
DIANA_ID = "22222222-dian-dian-dian-222222222222"
DYLAN_ID = "33333333-dyla-dyla-dyla-333333333333"


@pytest.fixture
def vault_dir():
    """Tempdir-rooted vault. EMI_WIKI_DIR is set so step_wiki_inference
    finds the prose pages we write."""
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
    """Seed Jukka + Diana with the kinship edge that lets the inference
    'Dylan is Jukka's nephew' work via Diana."""
    session = get_session()
    engine = session.bind
    session.close()
    KGMaintenanceFinding.__table__.drop(engine, checkfirst=True)
    KGMaintenanceFinding.__table__.create(engine, checkfirst=True)

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


# ── Stub agent ────────────────────────────────────────────────────────


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


# ── Tests ─────────────────────────────────────────────────────────────


def test_no_pages_means_no_subjects(vault_dir):
    """Empty vault → step returns zero work without ever creating an agent."""
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("agent must not be created when no pages"),
    ):
        result = run_wiki_inference(_ctx())
    assert result["subjects_examined"] == 0
    assert result["proposals_written"] == 0


def test_pick_only_pages_with_known_kg_node(vault_dir):
    """A wiki page exists for an entity not in the KG — it must be
    SKIPPED (no agent call wasted on a subject we can't anchor)."""
    _write_page(vault_dir, "RandomStranger", "x" * 500)

    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("no candidate, no agent"),
    ):
        result = run_wiki_inference(_ctx())
    assert result["subjects_examined"] == 0


def test_writes_proposal_for_dylan_kinship_inference(vault_dir):
    """Happy path: page about Jukka mentions 'Diana's son Dylan'. Agent
    proposes [Dylan nephew_of Jukka]. Step writes a claim_proposal."""
    page_text = (
        "# Jukka\n\n"
        "Jukka is the assistant's user. He has one sister, Diana, "
        "who has a son named Dylan. Dylan visits often and adores his uncle.\n\n"
        + "Filler. " * 60
    )
    _write_page(vault_dir, "Jukka", page_text)

    verdict = {
        "connections": [
            {
                "subject_node_id": JUKKA_ID,
                "target_node_id": None,           # Dylan not in KG yet
                "target_label": "Dylan",
                "target_node_type": "Person",
                "predicate": "nephew_of",
                "sentence": "Dylan is Jukka's nephew.",
                "evidence_quote": "Diana, who has a son named Dylan",
                "inference_path": "Page: Diana's son Dylan → KG: Diana sister_of Jukka → Dylan nephew_of Jukka.",
                "confidence": 0.9,
                "not_already_in_kg": True,
            }
        ],
        "reason": "One new kinship edge inferred from sibling+child structure.",
    }

    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())

    assert result["subjects_examined"] == 1
    assert result["proposals_written"] == 1
    assert result["proposals_rejected_low_confidence"] == 0
    assert result["proposals_rejected_already_in_kg"] == 0

    # Verify the proposal made it into claim_proposal*
    session = get_session()
    try:
        proposals = session.query(ClaimProposal).all()
        nodes = session.query(ClaimProposalNode).all()
        edges = session.query(ClaimProposalEdge).all()
    finally:
        session.close()

    assert len(proposals) == 1
    assert proposals[0].status == "pending"
    labels = {n.label for n in nodes if n.label}
    assert "Jukka" in labels
    assert "Dylan" in labels
    assert any(e.predicate == "nephew_of" for e in edges)


def test_low_confidence_proposals_skipped(vault_dir):
    """Below MIN_CONFIDENCE = 0.6 → silenced, not written."""
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)

    verdict = {
        "connections": [{
            "subject_node_id": JUKKA_ID,
            "target_label": "MaybeColleague",
            "target_node_type": "Person",
            "predicate": "works_with",
            "sentence": "Jukka may work with MaybeColleague.",
            "evidence_quote": "(speculative)",
            "inference_path": "guess",
            "confidence": 0.4,  # below threshold
            "not_already_in_kg": True,
        }],
        "reason": "speculative",
    }
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())

    assert result["proposals_written"] == 0
    assert result["proposals_rejected_low_confidence"] == 1
    session = get_session()
    try:
        assert session.query(ClaimProposal).count() == 0
    finally:
        session.close()


def test_already_in_kg_proposals_skipped(vault_dir):
    """If the agent's not_already_in_kg flag is False, skip without
    writing — and also verify-by-SQL: even if the agent says True, an
    existing same-predicate edge should make the step skip."""
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)

    # Add a Diana edge that would conflict with the agent's proposal
    session = get_session()
    try:
        session.add(Edge(
            id="cccccccc-cccc-cccc-cccc-edge0000cccc",
            source_id=DIANA_ID, target_id=JUKKA_ID,
            relationship_type="sibling_of",
            sentence="Diana is Jukka's sibling.",
        ))
        session.commit()
    finally:
        session.close()

    verdict = {
        "connections": [{
            "subject_node_id": JUKKA_ID,
            "target_node_id": DIANA_ID,
            "predicate": "sibling_of",  # already exists (Diana → Jukka)
            "sentence": "Jukka and Diana are siblings.",
            "evidence_quote": "his sister Diana",
            "inference_path": "Page says sister.",
            "confidence": 0.95,
            "not_already_in_kg": True,  # agent thinks new — but it exists
        }],
        "reason": "(false positive — verify-by-SQL should catch it)",
    }
    with _patch_agent(verdict):
        result = run_wiki_inference(_ctx())

    assert result["proposals_written"] == 0
    assert result["proposals_rejected_already_in_kg"] == 1


def test_short_page_skipped(vault_dir):
    """Pages shorter than 200 chars don't have enough context to reason on."""
    _write_page(vault_dir, "Jukka", "Jukka.")  # tiny

    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent"
    ) as mock_create:
        mock_create.return_value = StubAgent({"connections": [], "reason": "x"})
        result = run_wiki_inference(_ctx())

    # subject is examined (entered the loop) but page is too short to call agent
    assert result["proposals_written"] == 0


def test_subject_neighborhood_includes_existing_edge():
    """The neighborhood-formatter helper should render Diana sister_of Jukka."""
    block = _format_subject_neighborhood(JUKKA_ID)
    assert "Diana" in block
    assert "sister_of" in block


def test_edge_exists_helper_handles_id_or_label():
    """_edge_exists should recognize the seeded sister_of edge whether we
    pass target_id or target_label."""
    assert _edge_exists(
        subject_id=JUKKA_ID, target_id=DIANA_ID,
        target_label=None, predicate="sister_of",
    ) is True
    assert _edge_exists(
        subject_id=JUKKA_ID, target_id=None,
        target_label="Diana", predicate="sister_of",
    ) is True
    # Wrong predicate → not found
    assert _edge_exists(
        subject_id=JUKKA_ID, target_id=DIANA_ID,
        target_label=None, predicate="best_friend_of",
    ) is False


def test_sidecar_blocks_re_examination(vault_dir):
    """After examining a page, a sidecar marks it; a fresh run skips it
    until the page is re-written (mtime moves forward)."""
    _write_page(vault_dir, "Jukka", "Jukka. " * 200)
    verdict = {"connections": [], "reason": "nothing new"}
    with _patch_agent(verdict):
        first = run_wiki_inference(_ctx())
    assert first["subjects_examined"] == 1

    # Drop a sidecar that the picker would normally write itself; we
    # synthesize one to simulate a successful prior run.
    page = vault_dir / "prose" / "Jukka.md"
    sidecar = page.with_suffix(".wiki_inference.json")
    sidecar.write_text(
        json.dumps({"examined_at_epoch": page.stat().st_mtime + 1}),
        encoding="utf-8",
    )

    # Second run: nothing new
    with patch(
        "app.assistant.ServiceLocator.service_locator.DI.agent_factory.create_agent",
        side_effect=AssertionError("agent must not run when nothing fresh"),
    ):
        second = run_wiki_inference(_ctx())
    assert second["subjects_examined"] == 0
