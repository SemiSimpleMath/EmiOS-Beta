"""
Database models for the new KG chat projection source.

This table is a derived, chat-only projection from unified_log and is intended
to be the canonical input queue for the new production KG pipeline.
"""

import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Text, func

from app.models.base import Base


class KGChatProjection(Base):
    """Chat-only projection of unified_log + per-message entity resolution.

    Holds both the verbatim original message (`message`, never modified) and
    the LLM-resolved version (`resolved_text`, NULL until the resolver step
    runs). Single row per chat message; provenance preserved via
    `unified_log_id` + `message`.
    """

    __tablename__ = "kg_chat_projection"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Traceability back to immutable source-of-truth unified_log.
    unified_log_id = Column(String, nullable=False, unique=True, index=True)
    unified_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    source = Column(String, nullable=True, index=True)
    role = Column(String, nullable=True, index=True)

    # Verbatim original message text — copied from unified_log on filter,
    # NEVER modified afterward.
    message = Column(Text, nullable=False)

    # Speaker identity — carried forward from unified_log_2026 so downstream agents
    # know who actually spoke each turn. NULL for assistant turns and legacy rows.
    speaker_name = Column(String(256), nullable=True, index=True)
    room_id = Column(String(256), nullable=True, index=True)

    projection_version = Column(String(32), nullable=False, default="v1_chat_only")
    included_reason = Column(String(128), nullable=False, default="chat_role_allowed")

    # Reserved for downstream KG stages.
    processed = Column(Boolean, nullable=False, default=False, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Per-message entity resolution output (audit finding #12, 2026-04-25).
    # Populated by ResolveMessagesBatchedStep. Resolver does an UPDATE on
    # this row; never INSERTs a separate row, never modifies `message`.
    resolved_text = Column(Text, nullable=True)
    resolved_entities = Column(JSON, nullable=True)
    resolver_version = Column(String(32), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class KGChatConversationWindow(Base):
    __tablename__ = "kg_chat_conversation_window"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    start_projection_id = Column(String, nullable=False, index=True)
    end_projection_id = Column(String, nullable=False, index=True)
    start_unified_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    end_unified_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    message_count = Column(Integer, nullable=False)
    boundary_reason = Column(String(64), nullable=False, default="end_of_batch")
    status = Column(String(32), nullable=False, default="pending")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KGChatConversationWindowItem(Base):
    __tablename__ = "kg_chat_conversation_window_item"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    window_id = Column(String, nullable=False, index=True)
    projection_id = Column(String, nullable=False, unique=True, index=True)
    item_order = Column(Integer, nullable=False)
    role = Column(String, nullable=True)
    # Speaker display name resolved from unified_log_2026.speaker_name.
    # For role="user" this is the human participant's name (e.g. "Katy", "Phil").
    # NULL means unknown or assistant turn.
    speaker_name = Column(String(256), nullable=True)
    unified_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KGChatParsedSentence(Base):
    __tablename__ = "kg_chat_parsed_sentence"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    window_id = Column(String, nullable=False, index=True)
    sentence_order = Column(Integer, nullable=False)
    sentence = Column(Text, nullable=False)
    context = Column(Text, nullable=True)
    reasoning = Column(Text, nullable=True)
    parser_version = Column(String(64), nullable=False, default="knowledge_graph_add::parser")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_kg_chat_parsed_sentence_window_order", "window_id", "sentence_order"),
    )


class KGNodeEvidence(Base):
    """
    Append-only evidence trail for KG nodes.

    One row per observation — every time a conversation window produces or
    confirms a node, a row is appended here.  Old nodes have zero rows (honest
    NULL rather than fabricated data).  New nodes accumulate evidence from the
    first pipeline run that touches them.

    Provenance columns:
      source_table  — the log table the message came from, e.g. "unified_log_2026".
                      Stored verbatim so future table renames don't break old rows.
      source_id     — the UUID of the row in that table (stable identity).
      source_text   — verbatim raw-chat fragment from the parser's `context` field.
      derived_sentence — the agent-synthesised sentence that justified this node.
    """

    __tablename__ = "kg_node_evidence"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # FK to the final node
    node_id = Column(String, nullable=False, index=True)

    # Provenance — source log table + row id
    source_table = Column(String, nullable=True)   # e.g. "unified_log_2026"
    source_id = Column(String, nullable=True)       # unified_log row UUID

    # Evidence text
    source_text = Column(Text, nullable=True)       # verbatim fragment from parser context
    derived_sentence = Column(Text, nullable=True)  # agent-synthesised sentence

    # Temporal metadata
    message_timestamp = Column(DateTime(timezone=True), nullable=True, index=True)

    # Pipeline context
    window_id = Column(String, nullable=True, index=True)
    merge_action = Column(String(32), nullable=False, default="created")  # created | confirmed | updated

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_kg_node_evidence_node_ts", "node_id", "message_timestamp"),
        Index("ix_kg_node_evidence_source", "source_table", "source_id"),
    )


class KGEdgeEvidence(Base):
    """
    Append-only evidence trail for KG edges.

    Same design as KGNodeEvidence — one row per observation of a relationship.
    Edges have no timestamps today; this table closes that gap for all future
    observations.  Old edges have zero rows.
    """

    __tablename__ = "kg_edge_evidence"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # FK to the final edge
    edge_id = Column(String, nullable=False, index=True)

    # Provenance
    source_table = Column(String, nullable=True)
    source_id = Column(String, nullable=True)

    # Evidence text
    source_text = Column(Text, nullable=True)       # verbatim fragment from parser context
    derived_sentence = Column(Text, nullable=True)  # agent-synthesised sentence for this edge

    # Temporal metadata
    message_timestamp = Column(DateTime(timezone=True), nullable=True, index=True)

    # Pipeline context
    window_id = Column(String, nullable=True, index=True)
    merge_action = Column(String(32), nullable=False, default="created")  # created | confirmed | merged

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_kg_edge_evidence_edge_ts", "edge_id", "message_timestamp"),
        Index("ix_kg_edge_evidence_source", "source_table", "source_id"),
    )

