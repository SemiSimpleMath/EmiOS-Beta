"""SQLAlchemy model for the kg_revision_log table.

One row per KG mutation applied by ``kg_mutation_manager`` (or any other
authorized writer that records to this log). The log is the load-bearing
piece of "reversible auto" — every mutation snapshots the affected
rows before + after so the change can be reverted later.

op vocabulary (current):
  - "merge_nodes"          fold one node into another
  - "rename_label"         change a node's label (typo fix; never collides)
  - "update_node_field"    narrow per-field edit (alias add, date fix, etc.)
  - "delete_node"          remove a node + all its edges (full snapshot)
  - "delete_edge"          remove a single edge
  - "create_edge"          add one edge between existing nodes
  - "create_state_node"    add a State/Event node + its owner edge
  - "close_state"          set end_date on a State/Event node
  - "split_state"          undo a wrong-merge: replace one State/Event hub
                           with N new nodes, rerouting each edge to the
                           right new node. before_json captures the old
                           node + all its edges; after_json captures the
                           new node ids + their full snapshots so the
                           reverse op can rebuild the original hub.
  - "batch_text_substitute" apply N per-record text edits in one
                           transaction (e.g. replace 'age 15' with
                           'age 14' across N kg_node/kg_edge rows).
                           before_json + after_json each carry a list of
                           {table, id, field, value} for full revertibility.
                           See _execute_batch_text_substitute in
                           step_execute_findings.py.
  - "finding_resolve"      mark a kg_maintenance_finding as executed
  - "finding_escalate"     route a finding to user review

succeeded=0 rows record attempted-but-failed mutations with the error
in error_message. They are useful for retry / audit but did NOT change
the KG.

reverted_at + reverted_by are set when the mutation has been undone.
The original row stays for the audit trail; a new row is added for
the inverse op so the log reads forward.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Index, Integer, JSON, String, Text, func

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now


class KGRevisionLog(Base):
    __tablename__ = "kg_revision_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    op = Column(String(64), nullable=False, index=True)

    # Inputs the agent / caller supplied.
    args_json = Column(JSON, nullable=True)

    # Snapshots of affected rows. Both are JSON dicts/lists; format is
    # op-specific (merge_nodes captures both nodes + the rewritten edges;
    # rename_label captures just the node's label fields; etc.).
    before_json = Column(JSON, nullable=True)
    after_json = Column(JSON, nullable=True)

    reason = Column(Text, nullable=False)

    # Source finding (when the mutation came from a kg_maintenance_finding).
    finding_id = Column(String, nullable=True, index=True)

    # Which agent / route applied the mutation. Free-form string —
    # "kg_mutation::planner", "ui::merge_form", etc.
    agent_id = Column(String(128), nullable=True)

    # 1 = mutation committed to the KG. 0 = attempted-but-failed.
    succeeded = Column(Integer, nullable=False, default=1)
    error_message = Column(Text, nullable=True)

    # Set when this revision has been undone.
    reverted_at = Column(AwareUtcDateTime, nullable=True)
    reverted_by = Column(String(128), nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_kg_revision_log_finding", "finding_id"),
        Index("ix_kg_revision_log_op_created", "op", "created_at"),
    )
