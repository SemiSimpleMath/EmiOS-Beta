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
]


PodKind = Literal[
    "chat_cluster",
    "email",
    "image",
    "tool_result",
    "summary",
    "resource_snapshot",
]


class PodSourceRef(BaseModel):
    """A pointer from a pod back to the evidence it was built from."""
    kind: PodSourceKind
    id: str


class Pod(BaseModel):
    """Full in-memory shape of a pod row."""
    pod_id: str
    kind: str
    tags: List[str] = Field(default_factory=list)
    one_liner: str
    body: Optional[str] = None
    source_refs: List[PodSourceRef] = Field(default_factory=list)
    for_agents: List[str] = Field(default_factory=list)
    scope_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


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
