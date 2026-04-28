from typing import List, Optional

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    """
    A lightweight pointer back to source artifacts.
    Keep these short so a human can trace the claim.
    """

    ref: str = Field(
        description=(
            "A short evidence pointer. Prefer existing strings from the daily artifacts, "
            "e.g. a ticket_id, chat message_id, or a timeline item snippet like "
            "'ticket dayflow_meal_... dismissed/skip' or 'chat: <id>'."
        )
    )


class DominantTheme(BaseModel):
    theme: str = Field(description="What dominated the user's day/week-thinking in plain language.")
    why_it_matters: str = Field(description="Why this theme is important for understanding the user.")
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class HealthDayShape(BaseModel):
    baseline: str = Field(description="Baseline health/mental state for the day (1-2 sentences).")
    trend: str = Field(description="How the state changed over the day (1-2 sentences).")
    volatility: str = Field(description="Low/medium/high volatility with a brief justification.")
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class SleepSummary(BaseModel):
    summary: str = Field(description="Sleep summary relevant to this boundary-day.")
    key_numbers: List[str] = Field(
        default_factory=list,
        description="Short key stats as strings, e.g. 'total_sleep_minutes=420', 'quality=good'.",
    )
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class WorkPresenceSummary(BaseModel):
    summary: str = Field(description="AFK/active/work-session shape summary for the day.")
    key_numbers: List[str] = Field(default_factory=list, description="Key stats as short strings.")
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class TicketSummary(BaseModel):
    summary: str = Field(description="High-level ticket story: recurring, accepted/rejected, themes.")
    recurring: List[str] = Field(default_factory=list, description="Recurring ticket patterns (short).")
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class MemoryUpdateSummary(BaseModel):
    summary: str = Field(description="What preference/memory updates were applied (or failed).")
    applied_count: int = Field(default=0, description="How many insight items were attempted/applied.")
    failures: List[str] = Field(default_factory=list, description="Brief list of failures if any.")
    evidence: List[EvidenceRef] = Field(default_factory=list, description="Evidence pointers.")


class AgentForm(BaseModel):
    """
    A high-signal daily narrative summary for week/month rollups.
    """

    schema_version: int = Field(default=1)
    date: str = Field(description="Boundary-day date in YYYY-MM-DD.")
    timezone: str = Field(description="Local timezone name.")

    title: str = Field(description="Short title for the day.")
    executive_summary: List[str] = Field(
        default_factory=list,
        description="5-10 bullets of the most important points. No fluff.",
    )

    dominant_themes: List[DominantTheme] = Field(
        default_factory=list,
        description="Top 3-7 themes that dominated the day.",
    )

    health_day_shape: Optional[HealthDayShape] = Field(
        default=None,
        description="Compressed summary of dynamic health state across the day.",
    )

    sleep_summary: Optional[SleepSummary] = Field(
        default=None,
        description="Sleep story, including naps if present.",
    )

    work_presence_summary: Optional[WorkPresenceSummary] = Field(
        default=None,
        description="AFK/active session summary.",
    )

    tickets_summary: Optional[TicketSummary] = Field(
        default=None,
        description="Ticket outcomes and recurring patterns.",
    )

    memory_updates: Optional[MemoryUpdateSummary] = Field(
        default=None,
        description="Applied memory updates from daily insights.",
    )

    open_questions: List[str] = Field(
        default_factory=list,
        description="Questions to resolve ambiguity (keep short; only if needed).",
    )


# When agents are loaded dynamically, Pydantic may not automatically resolve
# nested annotations in some environments. Force rebuild defensively.
AgentForm.model_rebuild()

