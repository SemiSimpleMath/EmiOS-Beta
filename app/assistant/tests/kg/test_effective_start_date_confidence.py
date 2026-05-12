"""Test the chat-stamp guardrail in proposal_promoter.

Mirrors the audit-script logic (scripts/audit_actual_dates_no_explicit_evidence.py)
but runs at promote time so the bad label never lands on a fresh kg_node row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.kg.proposal_promoter import _effective_start_date_confidence


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kw):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, observed_dates):
        self._rows = [(datetime(*d, tzinfo=timezone.utc),) for d in observed_dates]

    def query(self, *_cols):
        return _FakeQuery(self._rows)


def _node(node_type, conf, valid_from):
    return SimpleNamespace(
        node_type=node_type,
        label="X",
        start_date_confidence=conf,
        valid_from=valid_from,
    )


def test_downgrade_when_start_date_equals_chat_date():
    sess = _FakeSession([(2026, 1, 15, 10, 30)])
    pn = _node("Event", "actual", datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "inferred"


def test_keep_actual_when_start_date_is_past_event_recall():
    # User said "we got married 2003-09-09" in a 2026-01-15 chat.
    sess = _FakeSession([(2026, 1, 15, 10, 30)])
    pn = _node("Event", "actual", datetime(2003, 9, 9, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "actual"


def test_no_change_for_already_inferred():
    sess = _FakeSession([(2026, 1, 15, 10, 30)])
    pn = _node("State", "inferred", datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "inferred"


def test_no_change_for_entity_node():
    sess = _FakeSession([(2026, 1, 15, 10, 30)])
    pn = _node("Entity", "actual", datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "actual"


def test_no_change_when_start_date_missing():
    sess = _FakeSession([(2026, 1, 15, 10, 30)])
    pn = _node("Event", "actual", None)
    assert _effective_start_date_confidence(sess, "pid", pn) == "actual"


def test_no_change_when_no_evidence_rows():
    sess = _FakeSession([])
    pn = _node("Event", "actual", datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "actual"


def test_downgrade_when_any_evidence_matches():
    # Multiple evidence rows; only one matches the start_date.
    sess = _FakeSession([
        (2026, 1, 14, 10, 30),
        (2026, 1, 15, 11, 0),
        (2026, 1, 16, 12, 0),
    ])
    pn = _node("Goal", "actual", datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert _effective_start_date_confidence(sess, "pid", pn) == "inferred"
