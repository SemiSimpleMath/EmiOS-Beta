"""SQLAlchemy model for kg_mention_map — durable mention resolutions.

Identity phase 5: when the node_merger CONFIRMS that a mention form
("the house", "Dave", "mom's place") refers to a specific node, and the
form is unambiguous graph-wide, that judgment is recorded HERE — so the
next mention of the same form binds closed-form instead of paying the
LLM confirm again.

This replaces what alias accretion tried to be, with the failure mode
designed out:
  - entries are minted ONLY from LLM-confirmed binds (never from
    string-match side effects),
  - ONLY for forms that are unambiguous graph-wide at mint time,
  - and REVOKED (revoked_at set, row kept for audit) the moment
    ambiguity arises — a new same-label node, a competing alias, a
    Disambiguation marker, or the target node disappearing.

A revoked form falls back to the normal resolution ladder, where the
confirm tier re-judges per mention — exactly the behavior the 2026-06-12
"House" capture incident taught us ambiguous forms need.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, Index, Integer, String, Text

from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base


class KGMentionMap(Base):
    __tablename__ = "kg_mention_map"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Normalized mention form (lowercased, whitespace-collapsed) + the
    # node_type scope it resolves within (a "House" Entity mention and a
    # "House" Concept mention are different questions).
    mention_norm = Column(String, nullable=False, index=True)
    node_type = Column(String(32), nullable=False)

    # The confirmed referent.
    node_id = Column(String, nullable=False, index=True)

    # Provenance: which confirm path minted this ("node_merger:alias_tier",
    # "node_merger:semantic_tier") and the proposal that carried the mention.
    minted_by = Column(String(64), nullable=False)
    source_proposal_id = Column(String, nullable=True)

    use_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(AwareUtcDateTime, nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)

    # Set when the form became ambiguous or the target vanished. Row kept
    # for audit; revoked forms fall back to the resolution ladder.
    revoked_at = Column(AwareUtcDateTime, nullable=True)
    revoked_reason = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_kg_mention_map_lookup", "mention_norm", "node_type", "revoked_at"),
    )
