from typing import List

from pydantic import BaseModel, Field


class MergeGroup(BaseModel):
    """A subset of the cluster's nodes that refer to the SAME real-world thing
    and should collapse into one canonical node."""

    canonical_node_id: str = Field(
        ...,
        description=(
            "id of the node to KEEP (the survivor). Pick the richest / "
            "most-connected / clearest-labeled one. Must be one of the ids shown."
        ),
    )
    duplicate_node_ids: List[str] = Field(
        default_factory=list,
        description=(
            "ids of the OTHER nodes in this group that merge INTO the canonical. "
            "Must contain at least one id and must NOT include the canonical id."
        ),
    )
    reason: str = Field("", description="<=1 sentence: why these are the same thing.")


class AgentForm(BaseModel):
    """Partition of ONE candidate duplicate cluster into true-duplicate groups
    (to merge) and distinct nodes (to keep separate).

    Coverage contract: every node id shown in the cluster must appear EXACTLY
    once across all merge_groups (as a canonical or a duplicate) and
    distinct_node_ids. Do not invent ids.
    """

    merge_groups: List[MergeGroup] = Field(
        default_factory=list,
        description=(
            "Groups of nodes that are genuinely the same thing. Empty list when "
            "nothing in the cluster should merge (a normal, expected result for "
            "clusters of distinct recurring occurrences)."
        ),
    )
    distinct_node_ids: List[str] = Field(
        default_factory=list,
        description=(
            "ids of nodes that are NOT duplicates of anything else in the cluster "
            "(e.g. separate dated occurrences of a recurring event) and must be "
            "kept separate."
        ),
    )
    reasoning: str = Field(
        "",
        description="<=3 sentences summarizing the cluster's nature and the key calls.",
    )
