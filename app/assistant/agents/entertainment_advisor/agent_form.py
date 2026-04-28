from typing import List, Optional

from pydantic import BaseModel, Field


class EntertainmentSuggestion(BaseModel):
    title: str = Field(
        description="Short title for the suggestion (e.g., 'Movie night: Dune Part 2').",
    )
    category: str = Field(
        description=(
            "Entertainment category. One of: movie, tv_show, music, game, "
            "book, podcast, outdoor_activity, social, hobby, dining, other."
        ),
    )
    reasoning: str = Field(
        description=(
            "Why this suggestion fits right now — reference time of day, mood, "
            "weather, health, schedule, or known preferences."
        ),
    )
    effort_level: str = Field(
        default="low",
        description="How much effort this requires: low, medium, high.",
    )
    time_estimate_minutes: Optional[int] = Field(
        default=None,
        description="Rough time commitment in minutes. Null if open-ended.",
    )
    requires_action: bool = Field(
        default=False,
        description=(
            "True if this needs the system to do something (e.g., order food, "
            "book tickets). False for pure suggestions the user acts on themselves."
        ),
    )
    action_description: str = Field(
        default="",
        description="If requires_action=true, what the system should do.",
    )


class AgentForm(BaseModel):
    no_action: bool = Field(
        default=False,
        description=(
            "True when no suggestion is warranted. User is busy, asleep, "
            "already entertained, or the timing is wrong. This is the correct "
            "answer most of the time."
        ),
    )
    no_action_reason: str = Field(
        default="",
        description="Brief reason when no_action=true.",
    )
    assessment: str = Field(
        default="",
        description=(
            "Brief assessment of the user's current entertainment state — "
            "what they seem to be doing, their energy level, available time."
        ),
    )
    suggestions: List[EntertainmentSuggestion] = Field(
        default_factory=list,
        description="At most 1-2 suggestions. Empty when no_action=true.",
    )
