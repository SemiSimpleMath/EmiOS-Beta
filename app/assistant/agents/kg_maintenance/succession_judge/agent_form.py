"""Output schema for kg_maintenance::succession_judge.

Diachronic referent check: do all of this role-reference entity's
evidence sentences describe ONE stable referent, or did the real-world
referent change at some point (era split needed)?"""
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(
        description=(
            "'stable' when all evidence plausibly describes one referent; "
            "'succession' when the referent clearly changed (a new school, a "
            "replaced car, a different employer team); 'unclear' when the "
            "evidence is too thin to tell."
        )
    )
    split_date: Optional[str] = Field(
        default=None,
        description=(
            "Required when verdict='succession': ISO date where the referent "
            "changed, floored per the year-floor doctrine ('fall 2024' -> "
            "2024-09-01, 'sometime in 2024' -> 2024-01-01). Use the evidence "
            "dates to bound it."
        ),
    )
    split_basis: Optional[str] = Field(
        default=None, max_length=300,
        description=(
            "Required when verdict='succession': WHICH evidence sentences "
            "imply the change and why the split_date follows from them."
        ),
    )
    era_before: Optional[str] = Field(
        default=None, max_length=200,
        description="When verdict='succession': one line describing the OLD era's referent.",
    )
    era_after: Optional[str] = Field(
        default=None, max_length=200,
        description="When verdict='succession': one line describing the NEW era's referent.",
    )
    confidence: str = Field(
        description="'high' | 'medium' | 'low' — how sure the verdict is."
    )
    reasoning: str = Field(
        max_length=500,
        description="Compact justification of the verdict.",
    )
