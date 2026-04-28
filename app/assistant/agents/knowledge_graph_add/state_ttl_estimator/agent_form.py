from typing import Literal, Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Duration estimate for a newly-observed state or event.

    The promoter stores this estimate on the node's attributes; a nightly
    decay job uses ``estimated_duration_days`` to auto-close the state's
    era when it expires without further observations.
    """

    duration_class: Literal[
        "ephemeral",   # hours to a day (moods, transient states)
        "short_term",  # days to weeks (acute illness, short trips)
        "medium_term", # weeks to months (projects, seasonal conditions)
        "long_term",   # months to years (jobs, dating relationships, interests)
        "durable",     # years or permanent (family relationships, diagnoses, beliefs)
    ] = Field(
        ...,
        description="Coarse bucket for the state's expected lifetime.",
    )

    estimated_duration_days: Optional[int] = Field(
        None,
        description=(
            "Specific expected duration in days from valid_from. Null for "
            "'durable' states that should never auto-close. Should be set "
            "for ephemeral/short/medium/long — pick the typical mid-point."
        ),
    )

    confidence: float = Field(
        ...,
        ge=0.0, le=1.0,
        description=(
            "0.0–1.0. Low confidence = the decay job should use a longer "
            "buffer before auto-closing. Below 0.4 means 'don't auto-close "
            "without a second opinion'."
        ),
    )

    reasoning: str = Field(
        ...,
        description="One-sentence justification for the chosen class + duration.",
    )
