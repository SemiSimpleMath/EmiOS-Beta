from typing import List

from pydantic import BaseModel, Field


class ActivityCount(BaseModel):
    activity: str = Field(description="The activity's field_name from the tracked activities, e.g. 'hydration'.")
    count: int = Field(description="Its NEW TOTAL count today, not the increment.")


class AgentForm(BaseModel):
    """Output schema for activity_tracker: the new totals of countable activities the chat shows happened, and the
    activities whose timer resets to now. Until 2026-11-15 `activity_counts` was a Dict[str, int], which OpenAI's
    structured output refuses ("'properties' is required for object schemas"), so every call failed."""

    activity_counts: List[ActivityCount] = Field(
        description="The countable activities the chat shows happened, each with its NEW TOTAL today. Empty when "
                    "none did."
    )
    activities_to_reset: List[str] = Field(
        description="field_names of activities the chat shows just happened; their timer resets to now (e.g. "
                    "['meal', 'finger_stretch'])."
    )
    reasoning: str = Field(description="Brief explanation of what evidence was found and what activities were "
                                       "detected")
