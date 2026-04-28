"""SQLAlchemy table for the pod store.

One row per pod. Minters compute deterministic pod_ids when the pod wraps
a single known source (same source → same id, inserts are idempotent).
Synthesized pods (agent summaries, multi-message cluster pods) use uuid
ids since there's no natural key.
"""
from __future__ import annotations

from sqlalchemy import Column, JSON, String, Text, TIMESTAMP, func

from app.models.base import Base


class PodRow(Base):
    __tablename__ = "pod_store"

    # Primary key — the datapod URI's opaque id. Deterministic when the pod
    # maps 1:1 to a source (e.g., datapod:email:<uid>); otherwise uuid.
    pod_id = Column(String, primary_key=True)

    # Controlled enum of pod shapes:
    # chat_cluster, email, tool_result, summary, resource_snapshot, ...
    kind = Column(String, nullable=False, index=True)

    # List of tag names from configs/pod_tags.yaml. Indexed for query by tag.
    tags_json = Column(JSON, nullable=False, default=list)

    # Load-bearing short description shown to consumers without hydration.
    one_liner = Column(Text, nullable=False)

    # Full content when small enough to inline. Nullable when the pod's
    # body is best resolved from source_refs (e.g., a big email body).
    body = Column(Text, nullable=True)

    # List of {kind, id} pointers back to evidence. At least one usually.
    # kind ∈ {"unified_log", "event_repository:email", "resource"}
    source_refs_json = Column(JSON, nullable=False, default=list)

    # Subscribers who care about this pod. Computed at mint time as the
    # union of agents whose pod_interest.tags intersects this pod's tags.
    # Denormalized for fast query-by-agent.
    for_agents_json = Column(JSON, nullable=False, default=list)

    # Scope binding. Pods inherit the originating room/scope; cross-scope
    # reads must be explicit. Null means system-wide.
    scope_id = Column(String, nullable=True, index=True)

    # Audit trail.
    created_by = Column(String, nullable=True)  # agent id or "pod_classifier"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())

    # Free-form, kind-specific structured fields (sender, email subject,
    # tool name, etc.). Not indexed — reserve for post-hoc inspection.
    metadata_json = Column(JSON, nullable=True)
