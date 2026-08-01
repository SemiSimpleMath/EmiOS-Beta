from pydantic import BaseModel, Field
from typing import List, Literal

EvidenceLabel = Literal["Reported", "Calendar", "Inferred"]


class ScheduleItem(BaseModel):
    """A single item on today's adjusted schedule."""
    title: str = Field(
        description="Short label for the schedule block (e.g., 'Work', 'Walk the dogs', 'Family time')."
    )
    start_local: str = Field(
        description="Start time in local 12-hour format (e.g., '9:00 AM'). Required."
    )
    end_local: str = Field(
        default="",
        description="End time in local 12-hour format (e.g., '4:30 PM'). Empty if open-ended."
    )
    status: Literal["upcoming", "ongoing", "completed", "cancelled"] = Field(
        description="Lifecycle status relative to current time."
    )
    source: str = Field(
        default="google_calendar",
        description=(
            "Short label for where this item came from / what last asserted it. "
            "Use compact tags so downstream readers can compare. "
            "Examples: 'google_calendar' (recurring or one-off Google event), "
            "'user_chat' (user reported it), 'concern:<concern_title_or_id>' "
            "(subconscious concern), 'inferred' (derived from telemetry). "
            "For an item asserted by a ticket response, use 'ticket:<ticket_id>' with "
            "the ticket_id copied VERBATIM from the Ticket Responses section — "
            "downstream readers resolve it to the originating work, exactly like "
            "calendar_item_id. When a newer signal overrides an older one, "
            "update source to the newer source."
        ),
    )
    updated_at_local: str = Field(
        default="",
        description=(
            "Local time this item was last asserted or updated, in 'YYYY-MM-DD HH:MM AM/PM' "
            "form. For Google Calendar items, this is the event's last-modified time; for "
            "user_chat-driven items, the message time; for concern-driven items, the concern's "
            "last-updated time. Empty if unknown. Used to resolve override conflicts — newer wins."
        ),
    )
    calendar_item_id: str = Field(
        default="",
        description="Stable calendar event ID. Copy verbatim from the input calendar event if source is 'google_calendar'. Leave empty for non-calendar items.",
    )


class Milestone(BaseModel):
    """A significant event that occurred today."""
    time: str = Field(
        description="Local time or local time range (e.g., '12:30 PM' or '12:35 PM - 1:20 PM')."
    )
    description: str = Field(
        description="Verbatim event description only (e.g., 'Lunch.' or 'AFK (ongoing).'). Do not include the evidence label in this field."
    )
    evidence: EvidenceLabel = Field(
        description="Evidence source: 'Reported' (user chat), 'Calendar' (calendar evidence), or 'Inferred' (telemetry)."
    )
    ongoing: bool = Field(
        default=False,
        description="True only if the milestone is currently ongoing (example: AFK ongoing)."
    )


class AgentForm(BaseModel):
    expected_schedule: List[ScheduleItem] = Field(
        default_factory=list,
        description=(
            "Today's adjusted schedule as a structured list. Start from calendar events, then adjust based on "
            "user reports and evidence (moved, cancelled, added items). Each item has local times — "
            "the pipeline will convert to UTC. Mark completed events as 'completed', cancelled as 'cancelled'."
        )
    )
    day_theme: str = Field(
        description="1 to 5 sentences describing what is dominating the day (plans, constraints, major concerns, travel day, etc.)."
    )
    milestones: List[Milestone] = Field(
        default_factory=list,
        description=(
            "Chronological historic log of significant events that already happened today. "
            "Include meals, naps, walks, major errands, major schedule changes, health updates, and AFK blocks of 20+ minutes. "
            "Exclude minor habits (water, coffee, stretches, bathroom trips, small snacks). "
            "AFK: at most one milestone per AFK interval; if ongoing and first reaches 20+ minutes, create one milestone at start time with ongoing=True; "
            "when it ends, edit that same milestone to final duration and set ongoing=False. "
            "You may revise an AFK milestone into a more specific event if later evidence explains what happened during that AFK."
        )
    )
    current_status: str = Field(
        description="Short label of what the user is doing right now (examples: 'At work', 'On lunch', 'AFK', 'Driving', 'At home')."
    )
