"""Tests for the specialized duplicate_node brief.

The user's principle (2026-05-07): NO deterministic merges. Every
duplicate_node finding goes through a high-powered investigator with
chat-window context. Recurring events (Friday-night dinners across
multiple weeks) look identical by label but must NOT be collapsed.

These tests cover the brief-builder side. The mutation-manager-side
guard (always escalate merge_nodes) lives in a prompt and is not
test-covered automatically — but the brief here ensures the
investigator has the evidence to make the right call.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_pipeline_models import KGWindow, KGWindowMessage
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg_investigator.finding_brief import build_finding_brief
from app.assistant.kg_maintenance.store import upsert_finding
from app.models.base import Base, get_session


# Stable test ids — keep them sorted so upsert_finding's pair-normalization
# leaves primary as primary (we want NODE_A < NODE_B alphabetically).
NODE_A = "11111111-1111-1111-1111-aaaaaaaaaaaa"  # earlier Friday Night Meats
NODE_B = "22222222-2222-2222-2222-bbbbbbbbbbbb"  # later Friday Night Meats
WINDOW_A = "wwwwwwww-wwww-wwww-wwww-aaaaaaaaaaaa"
WINDOW_B = "wwwwwwww-wwww-wwww-wwww-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _seed(kg_clean_db):
    """Recreate maintenance + chat-window tables and seed two near-identical
    Event nodes from chat windows five weeks apart — the recurring-event
    trap the brief is designed to catch."""
    session = get_session()
    engine = session.bind
    session.close()

    # The shared kg_clean_db only truncates a fixed list of tables; we need
    # to ensure the maintenance + window + evidence tables match the latest
    # schema for these tests.
    KGMaintenanceFinding.__table__.drop(engine, checkfirst=True)
    KGMaintenanceFinding.__table__.create(engine, checkfirst=True)
    Base.metadata.create_all(engine)  # adds the chat-window tables idempotently

    session = get_session()
    try:
        # Wipe + reseed the chat-side tables for a clean slate.
        for tbl in (KGNodeEvidence, KGWindowMessage, KGWindow, UnifiedLog2026):
            session.query(tbl).delete()

        # Two distinct Friday Night Meats nodes — same label, different occurrences.
        ts_a = datetime(2026, 1, 9, 19, 30, tzinfo=timezone.utc)  # Fri Jan 9
        ts_b = datetime(2026, 2, 13, 19, 30, tzinfo=timezone.utc)  # Fri Feb 13 — 5 weeks later

        session.add(Node(
            id=NODE_A, label="Friday Night Meats", node_type="Event",
            description="Friday-night dinner at a steakhouse",
        ))
        session.add(Node(
            id=NODE_B, label="Friday Night Meats", node_type="Event",
            description="Friday-night dinner at a steakhouse",
        ))

        # Conversation windows — earliest message is the window's "start"
        log_a_id = "log-jan9-1"
        log_b_id = "log-feb13-1"
        session.add(UnifiedLog2026(
            id=log_a_id, timestamp=ts_a, role="user", message="Heading to FNM tonight with Tom.",
            source="room_ui", processed=True, speaker_name="Jukka",
        ))
        session.add(UnifiedLog2026(
            id=log_b_id, timestamp=ts_b, role="user", message="FNM tonight — Anne and Sara joining.",
            source="room_ui", processed=True, speaker_name="Jukka",
        ))
        session.add(KGWindow(
            id=WINDOW_A,
            start_unified_log_id=log_a_id, end_unified_log_id=log_a_id,
            start_timestamp=ts_a, end_timestamp=ts_a, message_count=1,
            summary="Friday Night Meats with Tom — the Jan 9 occurrence.",
        ))
        session.add(KGWindow(
            id=WINDOW_B,
            start_unified_log_id=log_b_id, end_unified_log_id=log_b_id,
            start_timestamp=ts_b, end_timestamp=ts_b, message_count=1,
            summary="Friday Night Meats with Anne and Sara — the Feb 13 occurrence.",
        ))
        session.add(KGWindowMessage(window_id=WINDOW_A, unified_log_id=log_a_id, item_order=0))
        session.add(KGWindowMessage(window_id=WINDOW_B, unified_log_id=log_b_id, item_order=0))

        # Evidence rows linking each node to its window
        session.add(KGNodeEvidence(
            id=str(uuid.uuid4()), node_id=NODE_A, window_id=WINDOW_A,
            message_timestamp=ts_a, derived_sentence="Friday Night Meats with Tom",
        ))
        session.add(KGNodeEvidence(
            id=str(uuid.uuid4()), node_id=NODE_B, window_id=WINDOW_B,
            message_timestamp=ts_b, derived_sentence="Friday Night Meats with Anne and Sara",
        ))
        session.commit()
    finally:
        session.close()


def _make_dup_finding() -> str:
    fid, _ = upsert_finding(
        finding_type="duplicate_node",
        primary_node_id=NODE_A,
        secondary_node_id=NODE_B,
        suggested_action="merge",
        reason="Same label 'Friday Night Meats'; both Events.",
        confidence=0.95,
        priority="medium",
        agent_name="step_duplicate_scan",
    )
    return fid


def test_duplicate_brief_includes_chat_windows_for_both_nodes():
    fid = _make_dup_finding()
    result = build_finding_brief(fid)
    assert result is not None
    task, info = result

    # The whole point: BOTH chat windows must surface in the brief so the
    # investigator can see the actual conversations are different occurrences.
    assert "Chat-window evidence — PRIMARY" in info
    assert "Chat-window evidence — SECONDARY" in info
    assert "Heading to FNM tonight with Tom" in info
    assert "Anne and Sara joining" in info


def test_duplicate_brief_warns_about_recurring_events():
    """The task language must explicitly call out the recurring-event trap."""
    fid = _make_dup_finding()
    task, _ = build_finding_brief(fid)
    lower = task.lower()
    # Key concepts: recurring, repeated, weekly, distinct occurrences
    assert "recurring" in lower
    assert "occurrenc" in lower or "repeat" in lower
    # Decision rules must enumerate "distinct occurrences" → no_action
    assert "no_action" in lower or "no action" in lower
    assert "escalate_user" in lower or "escalate" in lower


def test_duplicate_brief_includes_window_summaries():
    """Each window's summary should make it clear this is week 1 vs week 5."""
    fid = _make_dup_finding()
    _, info = build_finding_brief(fid)
    assert "Jan 9 occurrence" in info
    assert "Feb 13 occurrence" in info


def test_duplicate_brief_works_when_one_side_has_no_windows():
    """Defensive: missing evidence on one side shouldn't crash; it's a real
    diagnostic state ('we don't have transcripts for the secondary')."""
    # Strip evidence for NODE_B
    session = get_session()
    try:
        session.query(KGNodeEvidence).filter(KGNodeEvidence.node_id == NODE_B).delete()
        session.commit()
    finally:
        session.close()

    fid = _make_dup_finding()
    task, info = build_finding_brief(fid)
    assert "Chat-window evidence — PRIMARY" in info
    assert "Chat-window evidence — SECONDARY" in info
    # The "no chat-window evidence" line should appear for the secondary.
    assert "no chat-window evidence" in info.lower()


def test_duplicate_brief_does_not_use_generic_node_pair_task_phrase():
    """The OLD generic _brief_node_pair task said 'Look for: alias overlap...
    Recommend merge_nodes ... or no_action'. The specialized brief MUST
    instead emphasize chat-window reading and recurring-event caution.
    Regression guard: if someone reverts back to the generic branch, this
    test catches it."""
    fid = _make_dup_finding()
    task, _ = build_finding_brief(fid)
    # The generic task said "Look for: alias overlap" — we replaced it.
    assert "Look for: alias overlap" not in task
    # The specialized task must contain the new disclosure phrasing.
    assert "READ BOTH CHAT-WINDOW" in task or "read both chat-window" in task.lower()
