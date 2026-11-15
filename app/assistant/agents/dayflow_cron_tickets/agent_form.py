from pydantic import BaseModel, Field
from typing import List


class StatusEffect(BaseModel):
    """A status field to update when suggestion is accepted."""
    activity_name: str = Field(
        description="Key of a tracked activity (one of the suggestion types listed in the system prompt) that the ticket records as done when accepted"
    )


class Suggestion(BaseModel):
    """A single proactive suggestion."""

    suggestion_type: str = Field(
        description="The tracked activity key this suggestion is for: one of the suggestion types listed in the system prompt. Any other type is discarded."
    )

    title: str = Field(
        description="Short title (e.g., 'Finger stretch time', 'Hydration reminder')"
    )

    message: str = Field(
        description="Conversational message spoken to the user. Friendly and natural."
    )

    priority: int = Field(
        description="1=most urgent, 5=least urgent, ranked by the Priority rule in the system prompt"
    )

    trigger_reason: str = Field(
        description="Brief reason why this is needed now"
    )

    status_effects: List[StatusEffect] = Field(
        default_factory=list,
        description="Tracked activities to record as done if the user accepts the ticket."
    )


class AgentForm(BaseModel):
    """
    Output from the proactive orchestrator agent.

    Returns ALL suggestions at once (not one at a time).
    The agent should analyze all needs and return them prioritized.
    Set no_action=True when no suggestions are warranted right now.
    """

    no_action: bool = Field(
        default=False,
        description=(
            "Set to true when no suggestions are warranted right now. "
            "When true, suggestions must be an empty list."
        ),
    )

    no_action_reason: str = Field(
        default="",
        description=(
            "Required when no_action is true. Brief reason why nothing is needed "
            "(e.g., 'all timers healthy, user in deep focus block')"
        ),
    )

    suggestions: List[Suggestion] = Field(
        description="List of all suggestions, ordered by priority (most urgent first). Empty list if nothing needed."
    )

    assessment: str = Field(
        description="One sentence summary of user's current state and needs"
    )
