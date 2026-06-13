"""Evidence digestion at threshold (fragility review #6 move 2, 2026-06-12).

A node crossing the live-row threshold gets its old evidence tail folded
into ONE consolidated digest row; originals are stamped digested_at and
kept (nothing deleted). Re-digestion folds the previous digest forward.

Uses the kg conftest (isolated test DB, fresh tables per test).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_core.kg_utils import evidence_digestion as mod
from app.assistant.kg_core.kg_utils.evidence_digestion import (
    DIGEST_THRESHOLD,
    KEEP_RECENT,
    MAX_FOLD_PER_NODE,
    run_evidence_digestion,
)
from app.models.base import get_session

T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mk_node(label="Heavy Node", identity_sentence=None):
    nid = str(uuid.uuid4())
    session = get_session()
    try:
        session.add(Node(id=nid, label=label, node_type="Entity",
                         identity_sentence=identity_sentence))
        session.commit()
    finally:
        session.close()
    return nid


def _mk_evidence(node_id, n, *, start=T0, sentence_prefix="obs"):
    session = get_session()
    try:
        for i in range(n):
            session.add(KGNodeEvidence(
                id=str(uuid.uuid4()), node_id=node_id,
                derived_sentence=f"{sentence_prefix} {i}",
                message_timestamp=start + timedelta(days=i),
            ))
        session.commit()
    finally:
        session.close()


def _rows(node_id, *, live=None, action=None):
    session = get_session()
    try:
        q = session.query(KGNodeEvidence).filter(KGNodeEvidence.node_id == node_id)
        if live is True:
            q = q.filter(KGNodeEvidence.digested_at.is_(None))
        elif live is False:
            q = q.filter(KGNodeEvidence.digested_at.isnot(None))
        if action:
            q = q.filter(KGNodeEvidence.merge_action == action)
        return q.all()
    finally:
        session.close()


def _patch_digester(monkeypatch, digest_text="Consolidated chronicle."):
    captured = []

    def fake(item):
        captured.append(item)
        return digest_text

    monkeypatch.setattr(mod, "_generate_digest", fake)
    return captured


def test_below_threshold_untouched(monkeypatch):
    nid = _mk_node()
    _mk_evidence(nid, DIGEST_THRESHOLD - 1)
    _patch_digester(monkeypatch)

    result = run_evidence_digestion()

    assert result["candidates"] == 0 and result["digested_nodes"] == 0
    assert len(_rows(nid, live=True)) == DIGEST_THRESHOLD - 1
    assert _rows(nid, action="digest") == []


def test_fold_creates_digest_and_stamps_tail(monkeypatch):
    nid = _mk_node(identity_sentence="The heavy node of the test graph.")
    n = DIGEST_THRESHOLD + 10
    _mk_evidence(nid, n)
    captured = _patch_digester(monkeypatch)

    result = run_evidence_digestion()
    assert result["digested_nodes"] == 1

    folded = n - KEEP_RECENT
    assert result["rows_folded"] == folded
    assert len(_rows(nid, live=False)) == folded
    live = _rows(nid, live=True)
    assert len(live) == KEEP_RECENT + 1  # recent verbatim + the digest row

    digest = _rows(nid, action="digest")[0]
    assert digest.derived_sentence == "Consolidated chronicle."
    assert digest.digested_at is None
    assert f"digest of {folded} evidence rows" in digest.source_text
    # Boundary timestamp: the digest sorts right before the surviving rows.
    assert digest.message_timestamp == T0 + timedelta(days=folded - 1)

    # The agent saw the tail oldest-first with dates and identity context.
    item = captured[0]
    assert item["identity_sentence"] == "The heavy node of the test graph."
    assert len(item["rows"]) == folded
    assert item["rows"][0]["text"] == "obs 0"
    assert item["rows"][0]["date"] == T0.date().isoformat()


def test_redigestion_folds_previous_digest_forward(monkeypatch):
    nid = _mk_node()
    _mk_evidence(nid, DIGEST_THRESHOLD + 10)
    captured = _patch_digester(monkeypatch, "First digest.")
    run_evidence_digestion()

    # New observations arrive; the node crosses the threshold again.
    _mk_evidence(nid, DIGEST_THRESHOLD, start=T0 + timedelta(days=400),
                 sentence_prefix="later")
    captured.clear()
    monkeypatch.setattr(mod, "_generate_digest",
                        lambda item: (captured.append(item), "Second digest.")[1])
    result = run_evidence_digestion()
    assert result["digested_nodes"] == 1

    # Exactly ONE live digest row; the first digest is itself digested.
    digests = _rows(nid, action="digest")
    live_digests = [d for d in digests if d.digested_at is None]
    assert len(digests) == 2 and len(live_digests) == 1
    assert live_digests[0].derived_sentence == "Second digest."

    # The previous digest was an input, flagged so the prompt carries it.
    kinds = [r["kind"] for r in captured[0]["rows"]]
    assert kinds[0] == "digest"


def test_chunking_caps_fold_per_run(monkeypatch):
    nid = _mk_node()
    n = MAX_FOLD_PER_NODE + KEEP_RECENT + 50
    _mk_evidence(nid, n)
    _patch_digester(monkeypatch)

    result = run_evidence_digestion()

    assert result["rows_folded"] == MAX_FOLD_PER_NODE
    assert len(_rows(nid, live=False)) == MAX_FOLD_PER_NODE
    assert len(_rows(nid, live=True)) == n - MAX_FOLD_PER_NODE + 1


def test_failed_digester_writes_nothing(monkeypatch):
    nid = _mk_node()
    _mk_evidence(nid, DIGEST_THRESHOLD)

    def boom(item):
        raise RuntimeError("llm down")

    monkeypatch.setattr(mod, "_generate_digest", boom)
    result = run_evidence_digestion()

    assert result["failed"] == 1 and result["digested_nodes"] == 0
    assert len(_rows(nid, live=True)) == DIGEST_THRESHOLD
    assert _rows(nid, action="digest") == []


def test_empty_digest_writes_nothing(monkeypatch):
    nid = _mk_node()
    _mk_evidence(nid, DIGEST_THRESHOLD)
    _patch_digester(monkeypatch, "")

    result = run_evidence_digestion()

    assert result["failed"] == 1 and result["digested_nodes"] == 0
    assert len(_rows(nid, live=True)) == DIGEST_THRESHOLD
