"""
SQLAlchemy model for the KG merge log — the undo playbook for every node merge.

One row per merge. Records enough state for a future unmerge to fully reverse
the operation:

  - loser_snapshot_json     → full pre-merge row for the node that was deleted
  - winner_pre_snapshot_json → full pre-merge row for the winner (because the
                              merge mutated the winner's fields — aliases,
                              description, etc.)
  - rerouted_edge_ids_json  → edges that were moved from loser → winner
  - dropped_edge_snapshots_json → edges that were dropped as duplicates during
                              rerouting (their full data, so unmerge can
                              re-INSERT them)
  - rebinds_json            → list of {table, column, row_id, old_node_id,
                              new_node_id} recording every dependent-row
                              pointer update

``undone_at`` is set if unmerge runs; otherwise null. ``merge_actor`` is the
caller identifier (pipeline name, 'ui', etc.) for audit. ``notes`` is a free
string for context.

This log is the only artifact of the deleted loser node after a merge. Do not
truncate it without a reason — you'd be destroying the only way to undo.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Index, JSON, String, Text, func

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime


class KGMergeLog(Base):
    __tablename__ = "kg_merge_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Parties ───────────────────────────────────────────────────────────────
    loser_id = Column(String, nullable=False, index=True)
    winner_id = Column(String, nullable=False, index=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    merge_actor = Column(String(128), nullable=True)
    notes = Column(Text, nullable=True)

    # ── Snapshots for undo ────────────────────────────────────────────────────
    loser_snapshot_json = Column(JSON, nullable=False)
    winner_pre_snapshot_json = Column(JSON, nullable=False)
    rerouted_edge_ids_json = Column(JSON, nullable=False, default=list)
    dropped_edge_snapshots_json = Column(JSON, nullable=False, default=list)
    rebinds_json = Column(JSON, nullable=False, default=list)

    # ── Audit ─────────────────────────────────────────────────────────────────
    merged_at = Column(
        AwareUtcDateTime, nullable=False, server_default=func.now()
    )
    undone_at = Column(AwareUtcDateTime, nullable=True)
    undone_by = Column(String(128), nullable=True)
    undone_notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_kg_merge_log_merged_at", "merged_at"),
        Index("ix_kg_merge_log_winner_loser", "winner_id", "loser_id"),
    )
