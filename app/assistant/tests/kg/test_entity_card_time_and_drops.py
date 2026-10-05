"""Entity cards: when each fact was said, the drop ledger, and the user-self gate (2026-10-05).

A friend's card read "regular Thursday evening Zoom call" for months after the call moved to Mondays: the card
writers never saw when a fact was said, a fact the renderer had dropped fell through to general_facts on every
build, and Node.first_observed carried the legacy import's date instead of the message's. Pins: a fact carries the
first and last date its source messages were written (kg_node_evidence, not first_observed) and the renderer is
shown them; a fact whose card tags were all dropped is skipped while an untagged one still defaults to
general_facts; the user-self gate reads the canonical primary user name.

Uses the kg conftest (isolated test DB, fresh tables per test). Invented data only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.database.kg_chat_projection import KGNodeEvidence
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node, NodeSectionTag
from app.assistant.kg.section_tagging import CARD_BUILDER_VERSION, NAMESPACE_CARD, node_content_hash
from app.assistant.pipelines.entity_cards_v2 import builder
from app.models.base import get_session

D = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def _no_tags():
    s = get_session()
    try:
        s.query(NodeSectionTag).delete()
        s.commit()
    finally:
        s.close()
    yield


def _node(label, node_type="State", said=(), **fields):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type=node_type, **fields))
        for when in said:
            s.add(KGNodeEvidence(node_id=nid, source_table="unified_log_2026", message_timestamp=when))
        s.commit()
    finally:
        s.close()
    return nid


def _edge(src, tgt, rel="has_state"):
    s = get_session()
    try:
        s.add(Edge(id=str(uuid.uuid4()), source_id=src, target_id=tgt, relationship_type=rel, sentence=rel))
        s.commit()
    finally:
        s.close()


def test_each_fact_carries_when_it_was_said_from_its_messages_not_first_observed():
    friend = _node("Pat", node_type="Entity")
    thursday = _node("Recurring Call", said=[D(2025, 1, 2)], first_observed=D(2026, 5, 12),
                     original_sentence="Pat and the owner have a call every Thursday.")
    monday = _node("Monday Call", said=[D(2026, 3, 3), D(2026, 9, 22)],
                   original_sentence="The call with Pat is on Mondays.")
    for n in (thursday, monday):
        _edge(friend, n)
    s = get_session()
    try:
        facts = builder._walk_candidate_facts(s, s.get(Node, friend))
    finally:
        s.close()
    said = {f["object_label"]: f["said"] for f in facts}
    assert said["Recurring Call"] == {"first": "2025-01-02", "last": "2025-01-02"}, "the message date, not the import"
    assert said["Monday Call"] == {"first": "2026-03-03", "last": "2026-09-22"}
    assert builder._said_line(said["Monday Call"]) == "said: first 2026-03-03, last 2026-09-22"
    assert builder._said_line(None) == "said: (no source message date)"


def test_the_renderer_is_shown_when_each_fact_was_said(monkeypatch):
    seen = {}

    class Agent:
        def action_handler(self, msg):
            seen.update(msg.agent_input)
            return type("R", (), {"data": {"bullets": []}})()
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: Agent())
    entity = Node(id="e", label="Pat", node_type="Entity")
    fact = {"fact_id": "f1", "direction": "outgoing", "edge_type": "has_state", "object_label": "Monday Call",
            "edge_sentence": "x", "object_sentence": "y", "said": {"first": "2026-03-03", "last": "2026-09-22"},
            "temporal": {"start_date": None, "end_date": None, "valid_during": None}}
    builder._run_bullet_renderer(entity, "general_facts", [fact])
    assert "said: first 2026-03-03, last 2026-09-22" in seen["source_facts"]


def test_a_fact_whose_card_tags_were_all_dropped_is_skipped_and_an_untagged_one_defaults():
    dropped = _node("Recurring Call", original_sentence="A call every Thursday.")
    untagged = _node("Chess", original_sentence="They play chess.")
    live = _node("Friendship", original_sentence="Old friends.")
    s = get_session()
    try:
        d = s.get(Node, dropped)
        s.add(NodeSectionTag(node_id=dropped, namespace=NAMESPACE_CARD, section_name="notes",
                             dropped_at=D(2026, 5, 22), dropped_at_node_content_hash=node_content_hash(d),
                             dropped_by_version=CARD_BUILDER_VERSION))
        s.add(NodeSectionTag(node_id=live, namespace=NAMESPACE_CARD, section_name="notes"))
        s.commit()
        facts = [{"fact_id": "f1", "source_node_ids": [dropped]}, {"fact_id": "f2", "source_node_ids": [untagged]},
                 {"fact_id": "f3", "source_node_ids": [live]}]
        tags = builder._tags_from_persisted(s, facts, ["notes", "general_facts"])
    finally:
        s.close()
    assert tags == [{"fact_id": "f2", "section": "general_facts"}, {"fact_id": "f3", "section": "notes"}]


def test_a_dropped_fact_is_reconsidered_when_the_builder_version_changes():
    dropped = _node("Recurring Call", original_sentence="A call every Thursday.")
    s = get_session()
    try:
        d = s.get(Node, dropped)
        s.add(NodeSectionTag(node_id=dropped, namespace=NAMESPACE_CARD, section_name="notes",
                             dropped_at=D(2026, 5, 22), dropped_at_node_content_hash=node_content_hash(d),
                             dropped_by_version="v1"))
        s.commit()
        tags = builder._tags_from_persisted(s, [{"fact_id": "f1", "source_node_ids": [dropped]}],
                                            ["notes", "general_facts"])
    finally:
        s.close()
    assert CARD_BUILDER_VERSION != "v1" and tags == [{"fact_id": "f1", "section": "notes"}]


def test_the_user_self_gate_reads_the_primary_user_name(monkeypatch):
    monkeypatch.setenv("PRIMARY_USER", "Sam")
    assert builder._is_user_self_entity(Node(id="a", label="sam", node_type="Entity"))
    assert not builder._is_user_self_entity(Node(id="b", label="Pat", node_type="Entity"))
    assert not builder._is_user_self_entity(Node(id="c", label="Sam", node_type="State"))
