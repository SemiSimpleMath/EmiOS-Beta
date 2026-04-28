from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RevisedBelief(BaseModel):
    belief_key: str = Field(
        description=(
            "The belief_key of the belief being re-evaluated. Must match the key provided. "
            "If splitting into two beliefs, use the original key for the primary and a new "
            "dot-suffixed key for the secondary (e.g. routine.dog_walk.morning.katy_covers)."
        )
    )
    statement: str = Field(
        description=(
            "The fully revised statement. This is the authoritative rewrite — "
            "incorporate all relevant qualifiers, conditions, and edge cases surfaced "
            "by the complete evidence trail. Make it agent-ready and self-contained."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "Re-calibrated confidence based on the full evidence trail. "
            "high = strong consistent signal across many observations. "
            "medium = mixed or sparse signal. "
            "low = mostly contradicting evidence, belief is weakly held."
        )
    )
    scope: Literal["chronic", "temporary"]
    status: Literal["active", "contested", "deprecated"] = Field(
        description=(
            "active = belief is now well-understood, even if qualified. "
            "contested = evidence is genuinely split — no clear resolution yet. "
            "deprecated = evidence shows the belief is no longer true."
        )
    )
    action: Literal["rewrite", "qualify", "split", "deprecate", "confirm"] = Field(
        description=(
            "rewrite = the core statement was wrong or incomplete, full rewrite needed. "
            "qualify = the core is correct but conditions/exceptions need adding. "
            "split = the belief was too broad; produces two distinct beliefs. "
            "deprecate = the belief is no longer valid. "
            "confirm = evidence confirms the belief as-is, restoring confidence."
        )
    )
    conditions: Optional[str] = Field(
        default=None,
        description=(
            "Any explicit conditions under which this belief holds or does not hold. "
            "e.g. 'on weekdays when partner is home' or 'except during travel'. "
            "Use natural language."
        )
    )
    reasoning: str = Field(
        description=(
            "2–4 sentences explaining: what the evidence trail showed, "
            "what changed vs. the original statement, and why you chose this action."
        )
    )


class AgentForm(BaseModel):
    revised_beliefs: List[RevisedBelief] = Field(
        description=(
            "One entry per belief being re-evaluated. If splitting, include both beliefs. "
            "If the belief is confirmed unchanged, use action=confirm."
        )
    )
    reevaluation_notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional overall notes about this re-evaluation pass — "
            "e.g. patterns noticed across multiple beliefs, signals that need more data, "
            "or recommendations for future evidence collection."
        )
    )
