from typing import List, Optional

from pydantic import BaseModel, Field


class WeeklyTheme(BaseModel):
    theme: str = Field(description="A recurring or dominant pattern observed across the week.")
    frequency: str = Field(
        description="How many days or how often this theme appeared (e.g. '5 of 7 days', 'every evening')."
    )
    significance: str = Field(
        description="Why this pattern matters for the user's wellbeing, productivity, or preferences."
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Short references from daily summaries supporting this theme.",
    )


class BeliefCandidate(BaseModel):
    statement: str = Field(
        description="A factual, actionable belief derived from the weekly pattern (e.g. 'User only eats pancakes on Mondays')."
    )
    domain: str = Field(
        description="Domain: routine | health | food | general | work | communication | sleep"
    )
    confidence: str = Field(
        description="high | medium | low — based on strength and consistency of weekly evidence."
    )
    conditions: Optional[str] = Field(
        default=None,
        description="Any qualifying conditions (e.g. 'only on weekdays', 'except when traveling')."
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Specific cross-day evidence from the daily summaries.",
    )


class AgentForm(BaseModel):
    week_label: str = Field(
        description="Human-readable week label, e.g. 'Week of Feb 24 – Mar 2, 2026'."
    )
    executive_summary: List[str] = Field(
        description="5–8 bullet points capturing the most important patterns, shifts, or highlights of the week.",
    )
    dominant_themes: List[WeeklyTheme] = Field(
        description="2–6 dominant themes or recurring patterns across the week.",
    )
    sleep_pattern: Optional[str] = Field(
        default=None,
        description="Cross-day sleep pattern observation (e.g. average hours, quality trend, weekend vs weekday).",
    )
    health_pattern: Optional[str] = Field(
        default=None,
        description="Overall health and energy arc across the week.",
    )
    work_pattern: Optional[str] = Field(
        default=None,
        description="Work rhythm pattern: peak days, off days, session lengths, deep work vs scattered.",
    )
    belief_candidates: List[BeliefCandidate] = Field(
        default_factory=list,
        description=(
            "Cross-day patterns that warrant a new or updated belief in the belief engine. "
            "Only include if the pattern appeared on 3+ days or was explicitly stated multiple times. "
            "Do not repeat beliefs already obvious from individual daily insights."
        ),
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description="Unresolved questions or anomalies noticed across the week that warrant watching.",
    )
