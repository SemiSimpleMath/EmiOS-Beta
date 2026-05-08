"""
SQLAlchemy model for the entity card maintenance findings table.

One row per finding regardless of type.  The subject column is:
  entity_card_id  — always set (the card under review)

finding_type values:
  "broken_kg_link"    — source_node_id set but KG node no longer exists
  "no_kg_link"        — source_node_id is NULL
  "blank_content"     — summary is empty/trivially short or key_facts is empty
  "stale_content"     — KG node updated >30 days after card was last generated
  "low_confidence"    — confidence < 0.5
  "junk_name"         — entity_name matches known artifact patterns

status values:
  "pending"   — awaiting human review
  "approved"  — human approved the suggested_action; queued for execution
  "rejected"  — human dismissed this finding
  "executed"  — action has been carried out
  "snoozed"   — defer until next pipeline run (re-open on next scan)

suggested_action values:
  "deactivate"  — soft-delete the card (set is_active=False)
  "review"      — human should inspect and decide
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Float, Index, JSON, String, Text, func

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime


class EntityCardMaintenanceFinding(Base):
    __tablename__ = "entity_card_maintenance_finding"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Classification ────────────────────────────────────────────────────────
    finding_type     = Column(String(64),  nullable=False, index=True)
    status           = Column(String(32),  nullable=False, default="pending", index=True)
    priority         = Column(String(16),  nullable=False, default="medium",  index=True)

    # ── Subject ───────────────────────────────────────────────────────────────
    entity_card_id   = Column(String,      nullable=False, index=True)

    # ── Conclusion ────────────────────────────────────────────────────────────
    suggested_action = Column(String(64),  nullable=False)
    reason           = Column(Text,        nullable=True)
    confidence       = Column(Float,       nullable=True)

    # Human-readable snapshot so reviewers don't need to re-query the card table.
    evidence_json    = Column(JSON,        nullable=True)

    # ── Execution trace ───────────────────────────────────────────────────────
    executed_by      = Column(String(128), nullable=True)
    executed_at      = Column(AwareUtcDateTime, nullable=True)
    execution_notes  = Column(Text,        nullable=True)

    # ── Pipeline provenance ───────────────────────────────────────────────────
    pipeline_run_id  = Column(String,      nullable=True, index=True)

    # ── Audit ─────────────────────────────────────────────────────────────────
    created_at = Column(
        AwareUtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at = Column(
        AwareUtcDateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_ecmf_dedup",          "finding_type", "entity_card_id", "status"),
        Index("ix_ecmf_card_status",    "entity_card_id", "status"),
        Index("ix_ecmf_type_status",    "finding_type", "status"),
        Index("ix_ecmf_priority_status","priority",      "status"),
    )
