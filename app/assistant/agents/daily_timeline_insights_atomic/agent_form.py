from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Source(BaseModel):
    """One timeline item the insight rests on. The model cites the number; code restores the item."""
    item: int = Field(description="The number `n` of a supplied timeline item.")
    evidence_kind: Literal["said", "did"] = Field(
        description="said = the person's own words (a chat message, a typed ticket reply). "
                    "did = observed behaviour (a button pressed, a prompt snoozed or left unanswered, "
                    "activity or absence at a time).")


class ActionableItem(BaseModel):
    fact_summary: str = Field(
        description="ONE claim, as a declarative statement about the person or their world, "
                    "understood on its own. Any wish or judgment names who holds it; an event "
                    "carries its date.")
    tags: List[str] = Field(description="Tags from the supplied list.")
    temporal_scope: Literal["chronic", "daily", "historical"] = Field(
        description="chronic = a lasting fact, preference or routine, stated as lasting or repeated "
                    "across days. daily = true of this day or a bounded situation. historical = a "
                    "past fact worth keeping as history.")
    kind: Literal[
        "durable_fact",
        "stable_relationship",
        "stable_preference",
        "routine_pattern",
        "episodic_context",
        "transient_state",
    ] = Field(
        description="How long this stays useful to know: durable_fact = years (a birthday, an "
                    "allergy); stable_relationship = persistent ties; stable_preference = an "
                    "enduring taste or wish; routine_pattern = a habit while practised; "
                    "episodic_context = a dated event or bounded period, days to weeks; "
                    "transient_state = gone within a day.")
    sources: List[Source] = Field(
        min_length=1, description="The timeline items this insight rests on.")
    change_recommended: Optional[str] = Field(
        default=None,
        description="Optional, for the person reading the insights page: what this might change, "
                    "in one short sentence.")


class AgentForm(BaseModel):
    actionable_information: List[ActionableItem]
