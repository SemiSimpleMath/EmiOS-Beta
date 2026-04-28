from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class BeliefOutput(BaseModel):
    belief_key: str = Field(
        description=(
            "Stable snake_case slug for this belief. Use dot notation by domain. "
            "e.g. routine.dog_walk.morning, routine.standing_break.dinner_time. "
            "Must be stable across runs — if updating an existing belief, use its existing key."
        )
    )
    statement: str = Field(
        description=(
            "Full prose statement of the belief as an agent would read it. "
            "Should be self-contained, specific, and include any qualifiers. "
            "e.g. 'User usually takes the dogs out around 8am on weekdays; "
            "partner sometimes covers this — verify on days they are home.'"
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "high = explicit user statement or many consistent observations. "
            "medium = inferred from a few consistent signals. "
            "low = single observation or weak signal."
        )
    )
    scope: Literal["chronic", "temporary"] = Field(
        description="chronic = durable long-term belief. temporary = days/weeks horizon."
    )
    status: Literal["active", "contested", "deprecated"] = Field(
        default="active",
        description=(
            "active = current and reliable. "
            "contested = conflicting evidence, use with caution. "
            "deprecated = no longer believed to be true."
        )
    )
    action: Literal["create", "update", "deprecate", "no_change"] = Field(
        description=(
            "create = new belief not previously in the store. "
            "update = existing belief needs its statement or confidence revised. "
            "deprecate = belief is no longer valid based on evidence. "
            "no_change = existing belief is confirmed as-is, only increment observation count."
        )
    )
    evidence_refs: List[int] = Field(
        default_factory=list,
        description=(
            "1-based indices of the evidence items (from the numbered evidence block) "
            "that directly support or inform this belief. "
            "Only include items that actually contributed to forming or updating this belief. "
            "Do not include items that are unrelated even if they were in the same batch. "
            "Required for create and update actions. May be empty for no_change or deprecate."
        )
    )
    reasoning: str = Field(
        description="One or two sentences explaining why you chose this action and statement."
    )


class AgentForm(BaseModel):
    beliefs: List[BeliefOutput] = Field(
        description=(
            "All belief decisions for this evidence batch. "
            "Include one entry per belief affected by the evidence. "
            "Do not invent beliefs not supported by the evidence."
        )
    )
