"""Output schema for wellness_proposer.

Single broad body/wellness domain — workouts, sleep, recovery, hydration,
mobility breaks, meditation. Mints intention.wellness pods (one per
proposal) and one intention.wellness_set pod aggregating the run.

Acute medical (appointments, prescriptions, symptom investigation) is
NOT in scope — those become concerns that escalate to dayflow tickets,
not autonomous wellness proposals.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


WellnessKind = Literal[
    "workout",
    "sleep_routine",
    "recovery",
    "hydration",
    "active_break",
    "meditation",
    "mobility",
]
Intensity = Literal["low", "moderate", "high"]
Confidence = Literal["high", "medium", "low"]
Novelty = Literal["familiar", "variation", "novel"]
WorkoutType = Literal[
    "cardio_easy",
    "cardio_moderate",
    "cardio_hard",
    "strength",
    "mobility",
    "sport",
    "walk",
    "mixed",
]


class WellnessProposal(BaseModel):
    """One proposed wellness intention for the next 24-48h."""
    model_config = ConfigDict(extra="forbid")
    kind: WellnessKind
    actors: List[str] = Field(
        description="Whose wellness this is for. Usually ['Jukka']; family workouts may include others.",
    )
    date: str = Field(description="ISO date the intention applies to (YYYY-MM-DD).")
    proposed_start_local: Optional[str] = Field(
        default=None,
        description="Specific time if known (ISO with tz offset). Null for 'whenever today' nudges.",
    )
    duration_minutes: Optional[int] = Field(
        default=None, ge=1, le=480,
        description="Expected duration. Null for non-duration things like hydration nudges.",
    )
    summary: str = Field(
        max_length=200,
        description="One-line user-facing label. e.g. '30 min easy bike ride', 'Wind down by 10pm, screens off 9:30pm'.",
    )
    # workout-specific (null for non-workout kinds)
    workout_type: Optional[WorkoutType] = None
    intensity: Optional[Intensity] = Field(
        default=None,
        description="Set for workout and recovery kinds. Null otherwise.",
    )
    equipment_used: List[str] = Field(
        default_factory=list,
        description="What gear is needed. Empty list when none.",
    )

    source: Literal["concern_addressing", "routine_maintenance", "opportunity"]
    confidence: Confidence
    novelty: Novelty
    novelty_rationale: Optional[str] = Field(
        default=None, max_length=300,
        description="Required when novelty='novel'. Why try this new thing now?",
    )
    reasoning: str = Field(max_length=400)


class AgentForm(BaseModel):
    """Top-level wellness_proposer output."""
    model_config = ConfigDict(extra="forbid")
    proposals: List[WellnessProposal] = Field(default_factory=list)
    skipped_today: List[str] = Field(
        default_factory=list,
        description="Wellness activities deliberately NOT proposed today, with reason. e.g. 'No workout — Jukka had high-intensity ride yesterday and sleep was 5h last night; recovery day.'",
    )
    rest_day_advisory: Optional[str] = Field(
        default=None, max_length=400,
        description="Set when the day's overall recommendation is REST (no intensity, focus on sleep/hydration/light walk). Otherwise null.",
    )
    free_form_thinking: str = Field(max_length=1000)
