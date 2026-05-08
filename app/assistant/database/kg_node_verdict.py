"""SQLAlchemy model for the kg_node_verdict table.

A verdict is a durable record produced by the kg_investigation_manager
when it concludes that a finding needs no KG mutation (take_action=False).
The investigator's prose memo + categorical type are stored here so that
future agents (duplicate-finder, investigator on the next run, merge
proposal_promoter) can consult prior decisions without re-investigating
the same question.

Pairwise verdicts (verdict_type='distinct') are the main case: "do not
merge node X with node Y." They're stored with a canonical lexicographic
ordering (node_id_a < node_id_b) so a single index lookup suffices.

Single-node verdicts ('verified', 'false_positive', 'irrelevant',
'obsolete') store the subject in node_id_a with node_id_b NULL.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, Float, Index, String, Text, UniqueConstraint

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now


class KGNodeVerdict(Base):
    __tablename__ = "kg_node_verdict"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Subjects ──────────────────────────────────────────────────────────────
    # Pairwise verdicts: both columns set, with node_id_a < node_id_b
    # (lexicographic — see verdict_store.canonical_pair).
    # Single-node verdicts: node_id_a set, node_id_b NULL.
    node_id_a = Column(String, nullable=False, index=True)
    node_id_b = Column(String, nullable=True, index=True)

    # ── Verdict ───────────────────────────────────────────────────────────────
    # Closed vocabulary; see agent_form.AgentForm.verdict_type.
    verdict_type = Column(String(32), nullable=False, index=True)

    # Short imperative line, ~12 words, names node ids inline. The
    # investigator-facing API; what future agents read.
    memo = Column(Text, nullable=False)

    # Longer reasoning prose — the investigator's recommendation field.
    reasoning = Column(Text, nullable=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    source_finding_id = Column(String, nullable=True, index=True)
    decided_by = Column(String(128), nullable=False)
    confidence = Column(Float, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    # When non-null, this verdict has been overridden (e.g., by a later
    # investigation that concluded otherwise, or by a node deletion that
    # made the verdict moot). Filter on superseded_at IS NULL in lookups.
    superseded_at = Column(AwareUtcDateTime, nullable=True)
    superseded_reason = Column(Text, nullable=True)

    # ── Audit ─────────────────────────────────────────────────────────────────
    # Python-side default: server_default=func.now() on SQLite returns a
    # naive timestamp that bypasses AwareUtcDateTime's bind path, leaving
    # writes mixed-tz on disk. utc_now() always returns aware UTC.
    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_kg_verdict_pair", "node_id_a", "node_id_b"),
        Index("ix_kg_verdict_type_a", "verdict_type", "node_id_a"),
    )
