"""
SQLAlchemy ORM models for the belief engine tables.

These map to the same schema defined in schema.py (created by migrate.py),
so no DDL changes are needed — just an ORM layer over the existing tables.
"""
from __future__ import annotations

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, String, Text
from app.models.base import Base


class UserBelief(Base):
    __tablename__ = "user_beliefs"

    id                = Column(String, primary_key=True)
    domain            = Column(String, nullable=False)
    belief_key        = Column(String, nullable=False, unique=True)
    statement         = Column(Text, nullable=False)
    # Stored confidence — what the LLM assigned at extraction time. Kept for
    # historical reference; agents read `current_confidence_band` (the
    # computed evidence-weighted snapshot) instead.
    confidence        = Column(String, nullable=False)
    scope             = Column(String, nullable=False)
    status            = Column(String, nullable=False, default="active")
    # Owner lock: 1 = a manual correction made via /beliefs; the nightly updater,
    # decay, and canonicalize must not modify or deprecate this belief.
    locked            = Column(Integer, nullable=False, default=0)
    # Decay model v2 (2026-05-11): drives half-life.
    kind              = Column(String, nullable=True)
    conditions        = Column(Text, nullable=True)
    observation_count = Column(Integer, nullable=False, default=1)
    first_observed    = Column(String, nullable=True)
    last_confirmed    = Column(String, nullable=True)
    last_contradicted_at = Column(String, nullable=True)
    # Snapshot columns — written by RecomputeBeliefSnapshotStep nightly.
    current_support_weight       = Column(Float, nullable=True)
    current_contradiction_weight = Column(Float, nullable=True)
    current_net_weight           = Column(Float, nullable=True)
    current_confidence_band      = Column(String, nullable=True)
    created_at        = Column(String, nullable=False)
    updated_at        = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_user_beliefs_domain", "domain"),
        Index("idx_user_beliefs_status", "status"),
        Index("idx_user_beliefs_key",    "belief_key"),
        Index("ix_user_beliefs_kind",    "kind"),
        Index("ix_user_beliefs_current_band", "current_confidence_band"),
    )


class BeliefEvidence(Base):
    __tablename__ = "belief_evidence"

    id          = Column(String, primary_key=True)
    belief_id   = Column(String, ForeignKey("user_beliefs.id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String, nullable=False)
    source_date = Column(String, nullable=True)
    source_ref  = Column(String, nullable=True)
    signal_type = Column(String, nullable=False)
    summary     = Column(Text, nullable=False)
    raw_text    = Column(Text, nullable=True)
    weight      = Column(Float, nullable=False, default=1.0)
    # Decay model v2: 'support' / 'contradict' / 'qualify'. Derived from
    # signal_type if not supplied; kept as a distinct column so future LLM
    # output can specify a more nuanced valence than signal_type captures.
    valence     = Column(String, nullable=True)
    # Half-life used at evidence-creation time — for replay reproducibility
    # if HALF_LIFE_DAYS constants change later.
    half_life_days_snapshot = Column(Integer, nullable=True)
    # Provenance — which agent/step wrote this evidence row.
    extracted_by = Column(String, nullable=True)
    created_at  = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_belief_evidence_belief_id",   "belief_id"),
        Index("idx_belief_evidence_source_date", "source_date"),
        Index("idx_belief_evidence_signal_type", "signal_type"),
        Index("ix_belief_evidence_valence",      "valence"),
    )
