from typing import List

from pydantic import BaseModel, Field, model_validator


class MergeAction(BaseModel):
    """One confirmed duplicate pair. The detector sees exactly one
    candidate pair per call, shown as N1 and N2, so `merge` is always
    exactly those two refs (code maps them back to node ids and refuses
    any other ref)."""

    merge: List[str] = Field(
        description=(
            "Exactly the two refs from the input pair: N1 and N2 "
            "(order doesn't matter - the executor merges into the node with "
            "most connections)"
        ),
        min_length=2,
        max_length=2,
    )

    labels: List[str] = Field(
        description="The two nodes' labels, same order as `merge`",
        min_length=2,
        max_length=2,
    )

    reason: str = Field(
        description="Brief reason why these nodes should be merged (max 100 chars)",
    )

    @model_validator(mode="after")
    def _labels_align_with_merge(self):
        if len(self.labels) != len(self.merge):
            raise ValueError(
                "labels must have exactly one entry per ref in merge (1:1)."
            )
        return self


class AgentForm(BaseModel):
    """Form for duplicate detector analysis - merge actions only.

    Empty merge_actions = the pair is not a duplicate (the common case)."""
    reason: str
    merge_actions: List[MergeAction] = Field(
        description="At most one merge action for the shown pair; empty when not a duplicate",
        default_factory=list,
    )

    total_merges: int = Field(
        description="Total number of merge actions",
        default=0,
    )
