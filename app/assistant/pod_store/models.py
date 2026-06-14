"""SQLAlchemy tables for the pod store.

`PodRow` — one row per pod. Minters compute deterministic pod_ids when
the pod wraps a single known source (same source → same id, inserts are
idempotent). Synthesized pods (agent summaries, multi-message cluster
pods) use uuid ids since there's no natural key.

`PodProjection` — for identity/artifact-style pods that carry multiple
authority-banded views of the same underlying secret. A pod's
projections live in this side table; reads are authority-gated.

`PodAudit` — every authority-gated read/write produces an audit row.
"""
from __future__ import annotations

from sqlalchemy import (
    Column, JSON, String, Text, TIMESTAMP, Integer, Float, UniqueConstraint, Index, func,
)

from app.models.base import Base


class PodRow(Base):
    __tablename__ = "pod_store"

    # Primary key — the datapod URI's opaque id. Deterministic when the pod
    # maps 1:1 to a source (e.g., datapod:email:<uid>); otherwise uuid.
    pod_id = Column(String, primary_key=True)

    # Controlled enum of pod shapes:
    # chat_cluster, email, tool_result, summary, resource_snapshot,
    # identity (multi-projection secret pods),
    # artifact (filled forms / signed documents — courier-produced).
    kind = Column(String, nullable=False, index=True)

    # List of tag names from configs/pod_tags.yaml. Indexed for query by tag.
    tags_json = Column(JSON, nullable=False, default=list)

    # Load-bearing short description shown to consumers without hydration.
    one_liner = Column(Text, nullable=False)

    # Full content when small enough to inline. Nullable when the pod's
    # body is best resolved from source_refs (e.g., a big email body) OR
    # when this is an identity/artifact pod whose values live in
    # PodProjection rows.
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

    # Minimum scope authority required to read this pod's body. Defaults to
    # 50 (chat-surface). Identity and artifact pods set higher values; the
    # PodProjection side-table refines this further on a per-projection
    # basis. Authority bands defined in app.assistant.pod_store.authority.
    min_authority = Column(Integer, nullable=False, default=50)

    # Curation signal (0-10, NULL = unrated). Set at mint time, editable in the
    # pods UI. Sortable so it can rank pod_search / inform retention. Distinct
    # from min_authority (which is read-permission, not priority).
    importance = Column(Float, nullable=True, index=True)

    # Audit trail.
    created_by = Column(String, nullable=True)  # agent id or "pod_classifier"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())

    # Free-form, kind-specific structured fields (sender, email subject,
    # tool name, etc.). Not indexed — reserve for post-hoc inspection.
    metadata_json = Column(JSON, nullable=True)


class PodProjection(Base):
    """One projection (view) of a pod's underlying value.

    Identity pods (SSN, DOB, phone, account#) have multiple projections at
    different authority levels: the full value, last-4, redacted, format
    description, etc. Each projection is independently authority-gated so
    an agent can read `last4` while being forbidden from `full`.

    Storage is by indirection: the row holds a POINTER to where the value
    lives (env var, file path) — not the value itself — for high-authority
    projections. Low-authority derived projections (`redacted`, `format`)
    are stored inline as `plain_value` because they're not sensitive on
    their own.
    """
    __tablename__ = "pod_projection"

    id = Column(String, primary_key=True)         # uuid
    pod_id = Column(String, nullable=False, index=True)  # FK → pod_store.pod_id
    projection_name = Column(String, nullable=False)
        # 'full', 'last4', 'redacted', 'format', 'area_code', etc.
        # Vocabulary varies by pod type (see materializers/).

    min_authority = Column(Integer, nullable=False)
        # Caller's scope.authority must be >= this to fetch.

    storage_kind = Column(String, nullable=False)
        # 'plain' | 'env' | 'file'
        # 'plain' → value inlined in plain_value (low-authority projections).
        # 'env'   → env_ref names an environment variable; value resolved
        #           via os.environ[env_ref] at fetch time. Used for string
        #           secrets the user types into .env.
        # 'file'  → file_ref is a path under data/pod_secrets/; bytes
        #           resolved at fetch time. Used for binary artifact pods
        #           (filled forms, scanned IDs).

    plain_value = Column(Text, nullable=True)
    env_ref = Column(String, nullable=True)
    file_ref = Column(String, nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())

    __table_args__ = (
        UniqueConstraint('pod_id', 'projection_name',
                         name='uq_pod_projection_per_pod'),
    )


class PodAudit(Base):
    """Audit row written on every authority-gated pod operation.

    Records WHAT was accessed (pod + projection), WHO accessed (scope +
    agent), WHEN, and whether the access was allowed. The VALUE is never
    logged — only the projection name and storage_kind. For 'env' reads
    we log the env var name (confirms which secret was consumed) but
    never the env value.
    """
    __tablename__ = "pod_audit"

    id = Column(String, primary_key=True)         # uuid
    pod_id = Column(String, nullable=False, index=True)
    projection_name = Column(String, nullable=True)
    operation = Column(String, nullable=False, index=True)
        # 'fetch' | 'list' | 'put' | 'attach' | 'denied'

    caller_scope_id = Column(String, nullable=True)
    caller_scope_authority = Column(Integer, nullable=True)
    caller_agent = Column(String, nullable=True)
    caller_request_id = Column(String, nullable=True)

    outcome = Column(String, nullable=False)      # 'allowed' | 'denied'
    detail = Column(Text, nullable=True)           # storage_kind, env_ref name, reason

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())

