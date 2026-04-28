from typing import List, Optional

from pydantic import BaseModel, Field


class TimelineItem(BaseModel):
    start_time_local: str = Field(
        description="Local start time in HH:MM or a full timestamp like YYYY-MM-DD HH:MM."
    )
    end_time_local: Optional[str] = Field(
        default=None,
        description="Local end time in HH:MM or full timestamp. Omit if unknown or ongoing.",
    )
    label: str = Field(description="Concise label for the activity or event.")
    evidence: str = Field(description="Reported | Calendar | Inferred")
    ongoing: bool = Field(default=False, description="True if this block is ongoing.")


class AgentForm(BaseModel):
    date: str = Field(description="Date of the context in YYYY-MM-DD.")
    timezone: str = Field(description="Local timezone name, e.g., America/Los_Angeles.")
    timeline: List[TimelineItem] = Field(
        default_factory=list,
        description="Chronological timeline for the day with normalized time ranges.",
    )
    notes: str = Field(description="Brief notes about ambiguities or missing time info.")
