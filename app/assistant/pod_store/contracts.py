"""Pydantic types for the pod store — one row's worth of data.

Pod is the in-memory shape; the DB row uses the same fields under
``*_json`` columns for the JSON-typed ones.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


PodSourceKind = Literal[
    "unified_log",
    "event_repository:email",
    "resource",
    "image_file",
    # A pod built FROM other pods (a plan referencing its intentions, a
    # summary referencing its sources) points back with kind="pod",
    # id=<pod_id> — the first-class pod→pod reference.
    "pod",
]


class PodSourceRef(BaseModel):
    """A pointer from a pod back to the evidence it was built from."""
    kind: PodSourceKind
    id: str


class Pod(BaseModel):
    """Full in-memory shape of a pod row.

    Pod kinds are registered strings (configs/pod_kinds.json — PodStore.put
    refuses unregistered kinds on new pods); the kind grammar and the
    canonical id grammar live in ``pod_uri``.

    Pod→pod references: use ``source_refs`` with kind="pod" for evidence
    lineage, or the ``metadata.related_pods`` list-of-pod_ids convention for
    lighter same-run groupings (the *_set pods' proposal_pod_ids predate the
    convention and stay as-is).
    """
    pod_id: str
    kind: str
    tags: List[str] = Field(default_factory=list)
    one_liner: str
    body: Optional[str] = None
    source_refs: List[PodSourceRef] = Field(default_factory=list)
    # Informational only — the tag-subscription routing this denormalized
    # for was never adopted (no pod_interest declarers, no for_agent
    # queries); consumers find pods by kind/tags/scope instead.
    for_agents: List[str] = Field(default_factory=list)
    scope_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Optional override of PodRow.min_authority. If None at put time, the DB
    # default (50 / AUTH_CHAT) applies. Set higher for content pods that
    # carry sensitive payloads — e.g., HTTP response pods sealed via
    # http_request's response_pod_kind path.
    min_authority: Optional[int] = None
    # Curation signal (0-10). None = unrated. Stored sortable in the DB so it
    # can rank pod_search and inform retention. A separate axis from
    # min_authority (which is read-permission, not priority).
    importance: Optional[float] = None


class PodHeader(BaseModel):
    """Hydrated header attached to a Message when pod URIs appear in its text.

    Cheap retrieval payload — everything an agent needs to decide whether to
    fetch the full body, with no body attached. Populated by PodInjector.
    """
    pod_id: str
    kind: str
    tags: List[str] = Field(default_factory=list)
    one_liner: str
    scope_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    content_type: str = ""
