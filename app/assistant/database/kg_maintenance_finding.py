"""
SQLAlchemy model for the unified KG maintenance findings table.

One row per finding regardless of type.  The subject columns are:
  primary_node_id   — always set (the "problem" node)
  secondary_node_id — set only for merge findings (the "other" node in the pair)
  edge_id           — set only for edge-level findings

finding_type values:
  "duplicate_node"       — two nodes that should be merged
  "orphan_node"          — node with zero edges
  "duplicate_edge"       — multiple edges between the same (src, tgt, type)
  "missing_description"  — node lacks a description field
  "type_error"           — node has a malformed or wrong node_type
  "wiki_contradiction"   — wiki_consistency_critic flagged a page disagreeing
                           with the KG / profile / cards
  "state_auto_closed"    — state_decay job auto-closed a stale State/Event
                           era; surfaced for confirmation rather than action
  "state_missing_dates"  — bounded-category State/Event with NULL start_date.
                           Hidden from the main triage dashboard; surfaced on
                           /kg-maintenance/date-gaps and drained 1-3/day by
                           the kg_date_gap_drain routine into the
                           questioner_manager. Priority bin = top-2 sum of
                           connected entity pageranks; auto-promoted to high
                           if directly connected to the primary user.
  "single_target_succession" — proposal_promoter (audit P2.1): a proposed
                           single-target fact (spouse/employer/...) postdates
                           a still-open era. The proposal stays 'pending'
                           (re-evaluates each run); resolving = close the old
                           era, after which the held proposal promotes.
                           primary=subject node, secondary=existing target.
  "single_target_conflict" — proposal_promoter (audit P2.1): same-era double
                           assertion of a single-target fact with no dates to
                           order them. The conflicting edge was skipped (rest
                           of the group applied); a human/investigator picks
                           the right target.

  "synthetic_fact_proposal" — wiki_connection_investigator proposes a NEW
                           KG fact inferred from a wiki page. Reviewed via
                           the synthetic_fact_review inbox (manual; the
                           approved→extracted→promoted pipeline is TBD).
                           Excluded from the general backlog drain.

  "disambiguation_backlog" — a Disambiguation node (attachment point for
                           mentions whose referent was unknown at write
                           time) has accumulated edges. The investigator
                           determines each edge's true referent and
                           proposes kg_repoint_edge; unresolvable edges
                           stay attached (legitimate waiting state).
                           Raised by step_disambiguation_scan.

  Historical (not produced today):
    "suspect_node" — per-node LLM quality scan, sunsetted 2026-04-27. Existing
                     rows of this type may still exist in DB and remain valid;
                     no new ones are produced. Same diagnosis is now reached
                     via wiki_contradiction (the wiki critic sees the node in
                     context and flags issues holistically).
    "event_series_link" — series-link scan, producers deleted 2026-06-10
                     (audit P3.6). ~105 legacy rows remain valid; the
                     executor's _execute_series_link branch stays for them.

status values:
  "pending"        — awaiting investigation or human review
  "investigated"   — kg_investigation_manager has produced a report (see investigation_report_json)
  "executing"      — claimed by the finding executor; mutation in flight
                     (atomic investigated→executing claim prevents double
                     execution; stale claims >2h are released back)
  "approved"       — human approved the suggested_action; queued for execution
  "rejected"       — human dismissed this finding (terminal)
  "executed"       — action has been carried out (terminal)
  "dismissed"      — investigator verdict: no action needed; durable verdict
                     recorded in kg_node_verdict (terminal)
  "escalated"      — routed to human review: planner declined/failed,
                     self-report uncorroborated, stale recommendation, or
                     repeated investigation failures (terminal)
  "execute_error"  — legacy/UI execution path raised (terminal)

Terminal statuses are never overwritten by set_status without an explicit
allow_terminal_transition=True (see kg_maintenance.store.TERMINAL_STATUSES).
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Float, Index, JSON, String, Text, func

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now


class KGMaintenanceFinding(Base):
    __tablename__ = "kg_maintenance_finding"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Classification ────────────────────────────────────────────────────────
    finding_type = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    priority = Column(String(16), nullable=False, default="medium", index=True)

    # ── Subject — flexible across finding types ───────────────────────────────
    # primary_node_id is always set.
    # secondary_node_id is set for merge/duplicate_node findings (the pair partner).
    # edge_id is set for duplicate_edge findings.
    primary_node_id = Column(String, nullable=False, index=True)
    secondary_node_id = Column(String, nullable=True, index=True)
    edge_id = Column(String, nullable=True, index=True)

    # ── Agent conclusion ──────────────────────────────────────────────────────
    suggested_action = Column(String(64), nullable=False)
    # "merge", "delete", "retype", "add_description", "review"

    reason = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)

    agent_name = Column(String(128), nullable=True)

    # Raw context the agent saw — lets UI show reviewers why finding was raised
    # without re-querying KG.
    evidence_json = Column(JSON, nullable=True)

    # ── Investigation trace (kg_investigation_manager) ────────────────────────
    # Structured report produced by the investigator: {diagnosis, evidence,
    # proposed_action, open_questions}. Set when status='investigated'.
    investigation_report_json = Column(JSON, nullable=True)
    investigated_at = Column(AwareUtcDateTime, nullable=True)

    # ── Execution trace ───────────────────────────────────────────────────────
    executed_by = Column(String(128), nullable=True)
    executed_at = Column(AwareUtcDateTime, nullable=True)
    execution_notes = Column(Text, nullable=True)

    # ── Pipeline provenance ───────────────────────────────────────────────────
    pipeline_run_id = Column(String, nullable=True, index=True)

    # ── Cluster membership ───────────────────────────────────────────────────
    # When a finding is part of a cluster (multiple findings that turn out to
    # share one root question), the kg_finding_cluster_resolver agent picks
    # one as the lead and stamps every other member's superseded_by with the
    # lead's id. The maintenance UI hides superseded findings by default;
    # the lead surfaces them via "+N similar" on expand. Cascade-resolution:
    # when the lead's status changes, sweep flips siblings to match. Lead
    # findings carry the synthesized root_question + sibling_ids in
    # evidence_json.cluster.
    superseded_by = Column(String, nullable=True, index=True)

    # ── Audit ─────────────────────────────────────────────────────────────────
    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(
        AwareUtcDateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    __table_args__ = (
        # Fast lookup: "do we already have a pending finding for this node pair?"
        Index(
            "ix_kg_mf_dedup",
            "finding_type",
            "primary_node_id",
            "secondary_node_id",
            "status",
        ),
        Index("ix_kg_mf_primary_status", "primary_node_id", "status"),
        Index("ix_kg_mf_type_status", "finding_type", "status"),
        Index("ix_kg_mf_priority_status", "priority", "status"),
    )
