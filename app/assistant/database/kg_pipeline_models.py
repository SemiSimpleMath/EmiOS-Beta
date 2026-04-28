"""
SQLAlchemy models for the KG pipeline (window-based, 2026-04-25).

unified_log_id is the canonical identity end-to-end. There is no
intermediate "kg_message" copy of the chat-only filtered messages —
the resolver reads from unified_log_2026 directly with its eligibility
filter applied.

Each stage in the pipeline owns one output table. The output of stage N is
the input bucket of stage N+1. Workers query their input bucket via
LEFT JOIN against their output table — items not in the output are "ready
for me." See docs/architecture/09_KG_PIPELINE.md for the full design.

Tables in this file (in pipeline order):
    kg_resolved_message      Stage 1 output: entity-resolved per message
    kg_window                Stage 2 output: coherent conversation windows
    kg_window_message        Stage 2 output: window ↔ unified_log_id membership
    kg_window_extraction     Stage 3 output: critic verdict + raw nodes/edges
    kg_window_enrichment     Stage 3.5 output: per-component enriched nodes/edges

Stage 4 writes to claim_proposal* (defined in claim_proposals.py).
Stage 5 writes to kg_node_metadata + kg_edge_metadata (defined in kg.db.*).

Naming note: a "window" here is the LLM-segmented conversation unit
produced by Stage 2. The legacy time-windowed table
(`kg_chat_conversation_window`) is the older implementation kept as a
historical archive — both are conceptually windows, just produced by
different mechanisms.
"""
from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint, func

from app.models.base import Base


# ---------------------------------------------------------------------------
# Stage 1 output: kg_resolved_message
# Per-message entity-resolved text. 1:1 with unified_log_2026 (chat-eligible).
# ---------------------------------------------------------------------------
class KGResolvedMessage(Base):
    __tablename__ = "kg_resolved_message"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # The canonical message identity — points back to unified_log_2026.id.
    unified_log_id = Column(String, nullable=False, unique=True, index=True)
    # Denormalized for query convenience (avoids JOIN to unified_log_2026 for
    # cheap chronological scans by downstream stages).
    unified_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    resolved_text = Column(Text, nullable=False)
    resolved_entities = Column(JSON, nullable=True)   # reserved
    resolver_version = Column(String(32), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Stage 2 output: kg_window + kg_window_message
# Coherent conversation windows detected by conversation_boundary agent.
# A window spans 1+ messages; one message belongs to exactly one window.
# ---------------------------------------------------------------------------
class KGWindow(Base):
    __tablename__ = "kg_window"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    start_unified_log_id = Column(String, nullable=False, index=True)   # FK to unified_log_2026.id
    end_unified_log_id = Column(String, nullable=False, index=True)     # FK to unified_log_2026.id
    start_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    end_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    message_count = Column(Integer, nullable=False)

    summary = Column(Text, nullable=True)                              # one-line topic summary
    standalone = Column(Boolean, nullable=True)                        # does this stand on its own?
    related_previous_window_ids = Column(JSON, nullable=True)          # cross-link list
    reason_for_boundary = Column(Text, nullable=True)                  # debug aid

    segmenter_version = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KGWindowMessage(Base):
    __tablename__ = "kg_window_message"

    window_id = Column(String, nullable=False, primary_key=True, index=True)
    unified_log_id = Column(String, nullable=False, primary_key=True, unique=True, index=True)
    item_order = Column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Stage 3 output: kg_window_extraction
# Critic verdict + raw nodes/edges from fact_extractor.
# ---------------------------------------------------------------------------
class KGWindowExtraction(Base):
    __tablename__ = "kg_window_extraction"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    window_id = Column(String, nullable=False, unique=True, index=True)

    # 'extracted' | 'rejected' | 'skipped' | 'error'
    verdict = Column(String(32), nullable=False, index=True)
    verdict_reason = Column(Text, nullable=True)

    nodes = Column(JSON, nullable=True)   # list of extractor node dicts (raw)
    edges = Column(JSON, nullable=True)   # list of extractor edge dicts (raw)

    extractor_version = Column(String(64), nullable=True)
    critic_version = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Stage 3.5 output: kg_window_enrichment
# One row per connected component (= one future claim_proposal).
# Carries the same nodes/edges as the extraction but with metadata enrichment
# applied (dates, aliases, importance, etc.) by meta_data_add.
# ---------------------------------------------------------------------------
class KGWindowEnrichment(Base):
    __tablename__ = "kg_window_enrichment"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    window_extraction_id = Column(String, nullable=False, index=True)
    component_index = Column(Integer, nullable=False)

    enriched_nodes = Column(JSON, nullable=False)
    enriched_edges = Column(JSON, nullable=False)

    enricher_version = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("window_extraction_id", "component_index", name="uq_enrichment_extraction_component"),
    )
