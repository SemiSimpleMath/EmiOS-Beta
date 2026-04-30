"""
Database models for the live KG evidence-trail tables.

The legacy chat-projection / conversation-window / parsed-sentence models
that used to live here were retired in favor of the unified pipeline
schema (kg_window / kg_window_message / kg_resolved_message). What remains
is the append-only evidence trail tables — kg_node_evidence and
kg_edge_evidence — which are still actively written by the live pipeline
and read by KG diagnostics tooling.
"""

import uuid

from sqlalchemy import Column, DateTime, Index, String, Text, func

from app.models.base import Base


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

