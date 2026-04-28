from typing import List

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Edge pre-filter output: indices of edges worth keeping."""

    reasoning: str = Field(
        ...,
        description="Brief explanation of filtering strategy for this node",
    )
    keep_indices: List[int] = Field(
        ...,
        description="1-based indices of edges to keep from the input list",
    )
