"""
Claim proposals — the shadow KG (state-mediated design).

Extractions write a COHERENT GROUP (nodes + edges + evidence) to these
tables first. Promotion to ``kg_node_metadata`` / ``kg_edge_metadata`` is
separate and atomic per group: either the whole group promotes or none
of it does. This preserves the extractor's native shape —
``entity --edge--> state --edge--> entity`` — because flat single-edge
proposals would lose the state mediators that carry time intervals and
participant sets.

Four tables:

    claim_proposal          -- one row per extraction unit (the group)
    claim_proposal_node     -- one row per node in the group
    claim_proposal_edge     -- one row per edge in the group
    claim_proposal_evidence -- append-only observation log (one row per
                               time this group was observed in the wild)

Design notes:
- Nodes and edges within a group reference each other by ``temp_id``
  (stable within the group, not globally unique). Promotion resolves
  each ``temp_id`` → ``resolved_node_id`` in phase 1 and uses that map
  to build edges in phase 2.
- ``claim_proposal_node.node_type`` drives the merge policy: Entity
  nodes match on label + aliases, State/Event nodes require
  participants + valid_from (never label-only), Concept nodes are
  singletons. Catch-all-singleton bugs (Parenthood, Marriage) come from
  ignoring this distinction.
- Evidence table snapshots the full chat-context envelope per
  observation. Re-observing the same group appends an evidence row;
  the group itself is not duplicated.

See ``project_kg_proposals_redesign_pending`` memory for the full
design context and the reason the previous flat schema was dropped.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON,
    String, Text, func,
)

from app.models.base import Base


CLAIM_TYPES = [
    "declarative_singular",
    "durable",
    "ephemeral",
    "inferred",
    "unknown",
]


PROPOSAL_STATUSES = [
    "pending",         # fresh; not yet evaluated
    "enriching",       # in enrichment pipeline (temporal, entity, etc.)
    "pending_review",  # held for user review (high-stakes or perplexing)
    "promoted",        # all nodes + edges written to KG
    "abandoned",       # never corroborated, N days passed
    "contradicted",    # conflicts with existing fact, flagged
    "retracted",       # was promoted, then rolled back
]


SOURCE_TYPES = [
    "chat",
    "email",
    "document",
    "setup_wizard",
    "manual_ui",
    "import",
]


# Node-type vocabulary — matches what fact_extractor emits today.
NODE_TYPES = ["Entity", "State", "Event", "Goal", "Concept", "Property"]


# Resolution actions recorded on each proposal_node after promotion phase 1.
RESOLUTION_ACTIONS = [
    "pending",              # not yet processed
    "matched_existing",     # found an existing KG node, resolved_node_id set
    "created_new",          # created a fresh KG node (typical for State/Event)
    "skipped_locked",       # target was locked + conflicting; do not overwrite
    "held_needs_existing",  # Entity has no match and policy forbids creation
]


class ClaimProposal(Base):
    """One row per coherent extraction unit. Carries the group-level
    metadata; the nodes and edges live in child tables."""

    __tablename__ = "claim_proposal"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    status = Column(String(32), nullable=False, default="pending", index=True)
    claim_type = Column(String(32), nullable=False, default="unknown", index=True)
    confidence = Column(Float, nullable=True)

    # Best single-sentence summary of what this group claims. The extractor's
    # sentence field from the primary edge, or the resolved-text prose.
    representative_sentence = Column(Text, nullable=True)

    # Observation aggregation (derived from claim_proposal_evidence).
    observation_count = Column(Integer, nullable=False, default=0)
    first_observed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Derivation chains.
    # parent_proposal_id: enrichment created this proposal by refining another.
    # supersedes_proposal_id: user-correction replacing an earlier proposal.
    parent_proposal_id = Column(String, nullable=True, index=True)
    supersedes_proposal_id = Column(String, nullable=True, index=True)

    # Soft-delete: preserves the proposal row with a retraction marker.
    retracted_at = Column(DateTime(timezone=True), nullable=True)
    retraction_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_claim_proposal_status_type", "status", "claim_type"),
        Index("ix_claim_proposal_created", "created_at"),
    )


class ClaimProposalNode(Base):
    """One row per node in a proposal group.

    The UUID ``id`` is the canonical identifier — edges reference nodes
    by ``id`` directly (proper FK). The extractor's original temp label
    ("n1", "state_1") is kept on ``extractor_temp_id`` as a debugging
    trail only; nothing else reads it.

    Populated with extractor output; ``resolved_node_id`` +
    ``resolution_action`` are filled by the promoter.
    """

    __tablename__ = "claim_proposal_node"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    proposal_id = Column(
        String, ForeignKey("claim_proposal.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # The extractor's within-window label ("n1", "state_1"). Useful for
    # reading logs back against the raw extractor output. Not referenced
    # as a FK anywhere; edges use ``id`` instead.
    extractor_temp_id = Column(String(64), nullable=True)

    label = Column(Text, nullable=False)
    node_type = Column(String(32), nullable=False, index=True)
    category = Column(String(128), nullable=True)

    # Extractor's verbatim single-sentence description of what this node
    # claims. Used as the ``original_sentence`` on the KG node when the
    # promoter creates it from this proposal (first-observation provenance).
    # Matched-existing nodes keep their existing original_sentence; new
    # sentences for re-observed nodes go into the evidence tables instead.
    sentence = Column(Text, nullable=True)

    # Draft description from extractor; the real description gets generated
    # by the maintenance description_creator after promotion.
    description_draft = Column(Text, nullable=True)

    # JSON catch-all for extractor-produced attributes that don't map to a
    # first-class column. Merged into kg_node_metadata.attributes at promotion.
    attributes_json = Column(JSON, nullable=True)

    # Bitemporal intake — when was this node's state true in the world.
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    valid_from_prose = Column(Text, nullable=True)
    valid_to_prose = Column(Text, nullable=True)
    start_date_confidence = Column(String(32), nullable=True)
    end_date_confidence = Column(String(32), nullable=True)

    # Resolution outputs (filled during promotion).
    resolved_node_id = Column(String, nullable=True, index=True)
    resolution_action = Column(String(32), nullable=False, default="pending")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_claim_proposal_node_resolved", "resolved_node_id"),
        Index("ix_claim_proposal_node_extractor_temp", "proposal_id", "extractor_temp_id"),
    )


class ClaimProposalEdge(Base):
    """One row per edge in a proposal group. Source and target are
    direct FK references to ``claim_proposal_node.id`` — UUIDs, no
    temp-id lookup required.
    """

    __tablename__ = "claim_proposal_edge"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    proposal_id = Column(
        String, ForeignKey("claim_proposal.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    source_node_id = Column(
        String, ForeignKey("claim_proposal_node.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_node_id = Column(
        String, ForeignKey("claim_proposal_node.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    predicate = Column(String(128), nullable=False, index=True)
    bidirectional = Column(Boolean, nullable=False, default=False)

    sentence = Column(Text, nullable=True)

    # Resolution output (filled during promotion phase 2).
    resolved_edge_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "ix_claim_proposal_edge_endpoints",
            "proposal_id", "source_node_id", "target_node_id", "predicate",
        ),
    )


class ClaimProposalEvidence(Base):
    """Append-only observation log. One row per time the group was
    observed (first extraction + any subsequent reinforcements).

    Carries the full chat-context envelope snapshotted at write time so
    the history is queryable even if unified_log rows get archived.
    """

    __tablename__ = "claim_proposal_evidence"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    proposal_id = Column(
        String, ForeignKey("claim_proposal.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Source envelope.
    source_type = Column(String(32), nullable=False)
    source_identifier = Column(String, nullable=True)  # pod_uri / external id

    # Chat / room context snapshot (from unified_log_2026 at ingest).
    room_id = Column(String, nullable=True, index=True)
    room_surface = Column(String(32), nullable=True)
    room_context_id = Column(String, nullable=True)
    speaker_name = Column(String(256), nullable=True, index=True)
    speaker_role = Column(String(32), nullable=True)

    # Pipeline provenance.
    # ``window_id`` is shared between the legacy (kg_chat_conversation_window)
    # and current (kg_window) implementations — both are conceptually windows,
    # just produced by different chunking algorithms. Loaders pick the right
    # storage table by trying each in turn.
    window_id = Column(String, nullable=True, index=True)
    projection_id = Column(String, nullable=True, index=True)  # legacy only
    enrichment_id = Column(String, nullable=True, index=True)  # new pipeline
    # Always populated (immutable source of truth):
    unified_log_id = Column(String, nullable=True, index=True)
    raw_text = Column(Text, nullable=True)

    # Extractor provenance — lets us invalidate a cohort of proposals later
    # when a bad prompt version is found.
    extractor_agent_name = Column(String(128), nullable=True)
    extractor_model = Column(String(64), nullable=True)
    extractor_prompt_version = Column(String(32), nullable=True)
    extraction_run_id = Column(String, nullable=True, index=True)

    confidence_at_observation = Column(Float, nullable=True)

    # Bitemporal: when the source message was authored (valid time).
    observed_at = Column(DateTime(timezone=True), nullable=True)
    # Transaction time: when the evidence row was persisted.
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_claim_proposal_evidence_cohort", "extractor_model", "extractor_prompt_version"),
        Index("ix_claim_proposal_evidence_speaker_ts", "speaker_name", "observed_at"),
    )
