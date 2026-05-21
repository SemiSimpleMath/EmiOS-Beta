"""Output schema for romantic_proposer.

Proposes small, calibrated romantic-relationship intentions for the
next 7-14 days — date nights (out/in), small gestures, anniversary
prep, quality time. Acutely sensitive domain — bias toward small +
familiar; don't overreach on flimsy signal.

Mints intention.romantic pods (one per proposal) and one
intention.romantic_set pod aggregating the run.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


RomanticKind = Literal[
    "date_night_out",
    "date_night_in",
    "small_gesture",
    "trip",
    "anniversary_prep",
    "quality_time",
]
Confidence = Literal["high", "medium", "low"]
Novelty = Literal["familiar", "variation", "novel"]


class RomanticProposal(BaseModel):
    """One proposed romantic intention for the next 7-14 days."""
    model_config = ConfigDict(extra="forbid")
    kind: RomanticKind
    actors: List[str] = Field(
        description=(
            "Whose relationship this is for. Usually ['Jukka','Katy']. "
            "Don't include kids unless the kind is quality_time AND the "
            "proposal is genuinely family-inclusive."
        ),
    )
    date: str = Field(description="ISO date the intention applies to (YYYY-MM-DD).")
    proposed_start_local: Optional[str] = Field(
        default=None,
        description="ISO datetime with tz for time-bound intentions (date night, event). Null for same-day gestures.",
    )
    duration_minutes: Optional[int] = Field(
        default=None, ge=1, le=4320,  # cap = 3 days
        description="Expected duration. Null for items without a meaningful duration.",
    )
    summary: str = Field(
        max_length=200,
        description=(
            "One-line user-facing label. e.g. 'Date night at Roy's "
            "(booked Sat 6:30pm, sitter for kids)', "
            "'Pick up Katy's favorite coffee on the way back from gym'."
        ),
    )

    cost_estimate_usd: Optional[float] = Field(
        default=None, ge=0,
        description="Estimated total cost (date night out: dinner + babysitter). Null for free.",
    )
    requires_babysitter: bool = Field(
        default=False,
        description="True if kids need coverage during this. Drives advance planning.",
    )
    advance_required_days: int = Field(
        default=0, ge=0, le=180,
        description=(
            "How many days of lead time this realistically needs. 0 for "
            "same-day gestures, 1-3 for date night (restaurant + sitter), "
            "7+ for trips. Higher numbers should match dates further out."
        ),
    )

    source: Literal["concern_addressing", "routine_maintenance", "anniversary_prep", "opportunity"]
    confidence: Confidence
    novelty: Novelty
    novelty_rationale: Optional[str] = Field(
        default=None, max_length=300,
        description="Required when novelty='novel'. Why try this new thing now?",
    )
    reasoning: str = Field(max_length=400)


class AgentForm(BaseModel):
    """Top-level romantic_proposer output."""
    model_config = ConfigDict(extra="forbid")
    proposals: List[RomanticProposal] = Field(default_factory=list)
    skipped_today: List[str] = Field(
        default_factory=list,
        description="Reasons no proposal in a category that might have been expected. e.g. 'No date night proposed — already have one Saturday on calendar.'",
    )
    free_form_thinking: str = Field(max_length=1000)
