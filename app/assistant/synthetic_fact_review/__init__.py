"""Synthetic-fact review module.

Owns the user-in-the-loop pipeline for facts that AGENTS inferred but
that need human review before landing in the KG. Producers today:
  - wiki_connection_investigator (reads wiki pages, infers connections)
Future producers (planned):
  - graph-walking "find missing connections" agents
  - inference agents triggered by ingestion gaps
  - any source where an LLM proposes a fact the user should approve

Architectural separation from the chat ingestion path:

  - Producers write kg_maintenance_finding rows of finding_type
    'synthetic_fact_proposal' (NOT claim_proposal rows). evidence_json
    carries the proposed sentence, suggested dates, evidence quote,
    inference path, confidence, producer name, and review_status.
  - This module owns the review state machine + edit helpers + the
    approve/reject actions. It does NOT modify chat-pipeline code,
    does NOT add wiki_inference rows to unified_log_2026, does NOT
    add new statuses to claim_proposal.
  - On approval, the module calls a SHARED merge-decision helper
    (extracted from proposal_promoter — TBD) directly on the
    user-edited content. The chat pipeline doesn't know synthetic
    facts exist; promotion happens via a function call, not by
    routing through claim_proposal.

This package is intentionally small. The data model lives on
kg_maintenance_finding.evidence_json (no new tables). Routes/UI
are added incrementally as the review surface gets built.
"""
from app.assistant.synthetic_fact_review.store import (  # noqa: F401
    REVIEW_STATUSES,
    FINDING_TYPE,
    apply_user_edit,
    get_review_state,
    list_pending_review,
    mark_rejected,
)
