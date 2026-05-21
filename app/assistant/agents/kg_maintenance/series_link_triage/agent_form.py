from typing import List, Literal
from pydantic import BaseModel, ConfigDict, Field


Verdict = Literal["is_series", "not_series"]


class ClusterVerdict(BaseModel):
    """One verdict for a candidate event-series cluster.

    `verdict`:
      - "is_series"   → the N events are recurring instances of one
                         household-specific concept; link them to a
                         parent Entity (create if missing).
      - "not_series"  → either the events are distinct things that
                         happen to share a generic label (different
                         births of different people, etc.) OR the
                         shared label is too generic to deserve a
                         parent concept ("Question", "Greeting", etc.).

    Asymmetric trust mirrors duplicate_triage: the cost of an
    incorrect "is_series" verdict is moderate (wrong link is
    reversible) and the cost of an incorrect "not_series" is just a
    missed opportunity, so be conservative — only return "is_series"
    when the cluster is clearly a coherent recurring concept.
    """
    model_config = ConfigDict(extra="forbid")
    cluster_index: int = Field(
        ...,
        description="1-based index of the cluster in the input batch.",
    )
    verdict: Verdict = Field(
        ...,
        description="'is_series' or 'not_series'.",
    )
    canonical_label: str = Field(
        "",
        max_length=200,
        description=(
            "The concept name to use when verdict='is_series'. Usually "
            "the cluster's display label as-is. Empty string when "
            "verdict='not_series'."
        ),
    )
    reason: str = Field(
        "",
        max_length=300,
        description="≤1 sentence justification.",
    )


class AgentForm(BaseModel):
    """Batched triage output. One row per input cluster, in any order."""
    model_config = ConfigDict(extra="forbid")
    clusters: List[ClusterVerdict]
