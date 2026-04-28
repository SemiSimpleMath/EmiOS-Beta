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
    confidence        = Column(String, nullable=False)
    scope             = Column(String, nullable=False)
    status            = Column(String, nullable=False, default="active")
    conditions        = Column(Text, nullable=True)
    observation_count = Column(Integer, nullable=False, default=1)
    first_observed    = Column(String, nullable=True)
    last_confirmed    = Column(String, nullable=True)
    created_at        = Column(String, nullable=False)
    updated_at        = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_user_beliefs_domain", "domain"),
        Index("idx_user_beliefs_status", "status"),
        Index("idx_user_beliefs_key",    "belief_key"),
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
    created_at  = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_belief_evidence_belief_id",   "belief_id"),
        Index("idx_belief_evidence_source_date", "source_date"),
        Index("idx_belief_evidence_signal_type", "signal_type"),
    )
