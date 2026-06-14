"""Pydantic schemas for the mint_pod tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class mint_pod_args(BaseModel):
    """Input schema for mint_pod."""
    title: str = Field(
        ...,
        description="Short one-line title / summary — becomes the pod's one_liner (what pod_search shows).",
    )
    body: str = Field(
        ...,
        description="The full content to store verbatim as the pod body.",
    )
    kind: str = Field(
        default="note",
        description="Pod kind. Only 'note' (freeform authored content — lists, contacts, notes) is allowed.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Optional tag names for later pod_search filtering, e.g. ['contacts'].",
    )
    importance: Optional[float] = Field(
        default=None,
        description="Optional curation importance 0-10. Higher = surfaces first in search / retained longer. Omit if unsure.",
    )
    min_authority: Optional[int] = Field(
        default=None,
        description="Optional read-permission band: 10 public, 50 chat (default), 70 gated, 99 user-only. Omit for the 50 default.",
    )


class mint_pod_arguments(BaseModel):
    """Tool wrapper for mint_pod."""
    tool_name: str
    arguments: mint_pod_args


mint_pod_arguments.model_rebuild()
