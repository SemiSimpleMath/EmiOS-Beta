from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CanonicalBelief(BaseModel):
    belief_key: str = Field(
        description=(
            "The belief_key for this canonical belief. "
            "For merges: use the most specific, well-formed key from the cluster. "
            "For splits: you may derive a new key from the original (e.g. "
            "'health.sleep.bedtime_boundary' split from 'health.sleep_policy'). "
            "Keys must be dot-separated, lowercase, domain-prefixed."
        )
    )
    statement: str = Field(
        description=(
            "The statement for this canonical belief. Must be agent-ready and self-contained. "
            "Target 1–3 sentences, ~40–120 words. "
            "For merges: incorporate the best qualifiers from all beliefs in the cluster. "
            "For splits: cover only the single rule this record is responsible for."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "Use the highest confidence level from the source belief(s) if the statements are "
            "consistent. Drop one level if the cluster contains meaningful variation. "
            "high = strong consistent signal. medium = mixed. low = weak."
        )
    )
    scope: Literal["chronic", "temporary"]
    deprecated_keys: List[str] = Field(
        default_factory=list,
        description=(
            "belief_keys to deprecate after this canonical belief is written. "
            "For merges: the redundant keys from the cluster (not the surviving key). "
            "For splits: include the original oversized key in exactly one of the split outputs. "
            "May be empty."
        )
    )
    merge_reasoning: str = Field(
        description=(
            "2–4 sentences explaining what was done: "
            "for merges — what the beliefs had in common and what was synthesized; "
            "for splits — what independent rule this record covers and why it was separated."
        )
    )


class AgentForm(BaseModel):
    canonical_beliefs: List[CanonicalBelief] = Field(
        description=(
            "One entry per output belief — either a merge result or a split result. "
            "For merges: one entry per cluster consolidated, deprecating the redundant keys. "
            "For splits: one entry per sub-belief produced, with the original key deprecated in exactly one of them. "
            "Omit beliefs that need no change. Only emit entries where something actually changes."
        )
    )
    canonicalization_notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional overall notes — e.g. beliefs left as-is and why, "
            "splits that were considered but rejected, or patterns worth watching."
        )
    )
