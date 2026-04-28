from typing import List, Optional

from pydantic import BaseModel, Field


class EntertainmentSuggestion(BaseModel):
    title: str = Field(description="Short title for the suggestion.")
    tier: str = Field(
        description="One of: comfort, medium, push.",
    )
    category: str = Field(
        description=(
            "Entertainment category: movie, tv_show, music, game, "
            "book, podcast, outdoor_activity, social, hobby, dining, other."
        ),
    )
    reasoning: str = Field(
        description="Why this fits right now — reference preferences, timing, weather, mood.",
    )
    effort_level: str = Field(default="low", description="low, medium, high.")
    time_estimate_minutes: Optional[int] = Field(
        default=None,
        description="Rough time commitment. Null if open-ended.",
    )
    requires_action: bool = Field(
        default=False,
        description="True if the system needs to do something (order, book, send).",
    )
    action_description: str = Field(
        default="",
        description="If requires_action=true, what the system should do.",
    )
    feasibility_notes: str = Field(
        default="",
        description="Research notes on feasibility (hours, availability, location).",
    )


class FinalAnswerDataItem(BaseModel):
    data_type: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    url: Optional[str] = None
    link: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class AgentForm(BaseModel):
    assessment: str = Field(
        default="",
        description="Brief assessment of the user's current entertainment state.",
    )
    no_action: bool = Field(
        default=False,
        description="True when no suggestion is warranted.",
    )
    no_action_reason: str = Field(
        default="",
        description="Brief reason when no_action=true.",
    )
    suggestions: List[EntertainmentSuggestion] = Field(
        default_factory=list,
        description="Tiered suggestions. Empty when no_action=true.",
    )
    research_findings: List[str] = Field(
        default_factory=list,
        description="Key preference data discovered during research, to persist for future runs.",
    )
    future_questions: List[str] = Field(
        default_factory=list,
        description="Questions the manager should research next time.",
    )
    final_answer_task: Optional[str] = ""
    final_answer_answer: str = ""
    result_summary: Optional[str] = Field(
        default="",
        description="One-sentence outcome for downstream agents (max ~150 chars).",
    )
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
