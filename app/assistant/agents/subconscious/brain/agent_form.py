"""Output schema for subconscious::brain — what one matter changes in the household's concerns."""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# The brain raises concerns (until 2026-10-09 these types lived with the noticer, which raised them too).
ConcernKind = Literal["pattern_drift", "anticipated_need", "opportunity_external", "opportunity_internal",
                      "schedule_collision", "gift_opportunity"]
Severity = Literal["low", "medium", "high"]
Horizon = Literal["today", "this_week", "this_month", "long_horizon"]


class EventDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(description="The event's label exactly as given, e.g. E2.")
    decision: Literal["changes_concern", "new_concern", "no_concern"] = Field(
        description="changes_concern: it changes one open concern (a new fact, a new date, progress, or settles it); "
                    "your update of that concern records it. new_concern: a concern you raise comes from it. "
                    "no_concern: it changes no concern and raises none; the reason records what it established.")
    concern: Optional[str] = Field(
        default=None,
        description="changes_concern: the one concern it changes (e.g. C1). new_concern: the concern you raise from it "
                    "(e.g. N1). Empty for no_concern.")
    reason: str = Field(description="Why, specific to this event, in words; no labels.")


class ConcernUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concern: str = Field(description="The concern's label exactly as given, e.g. C1.")
    action: Literal["note", "resolve"] = Field(
        description="note: record what the events add (a new fact, a new date, a new plan, progress). "
                    "resolve: the concern is settled: its done-when is met, or it is cancelled or no "
                    "longer needed, or the owner says it is over.")
    note: str = Field(description="For note: the new facts, stated plainly. For resolve: why it is settled. "
                                  "Words and dates; no labels.")
    relies_on: List[str] = Field(description="The labels of what the note rests on: the events, and anything else "
                                             "shown (W, R, K).")
    attach: List[str] = Field(description="The events (E) whose message, email, sighting or report is kept as this "
                                          "concern's evidence. Empty when none is.")
    expires_at: Optional[str] = Field(
        default=None,
        description="Only when the events change when this concern stops mattering (the appointment moved): the new "
                    "local time, YYYY-MM-DD HH:MM. Empty otherwise.")


class NewConcern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="N1, N2, ... within this answer.")
    title: str = Field(max_length=120, description="One line, specific.")
    subject: Optional[str] = Field(default=None, description="Whose matter it is: a person's name, or 'household'.")
    kind: ConcernKind
    severity: Severity
    horizon: Horizon
    done_when: str = Field(
        description="The outcome that closes the concern, in one line. A delivery (a reminder sent) is "
                    "not an outcome. When the owner stated the condition, use the owner's words.")
    expires_at: str = Field(
        description="The local time after which this concern no longer matters, whatever happened, YYYY-MM-DD HH:MM: "
                    "for something only relevant today, a time today (the end of its moment, or midnight); otherwise "
                    "the time in the future when it stops mattering.")
    notes: str = Field(description="Why this matters to the household, and what is known so far, in words; no "
                                   "labels.")
    relies_on: List[str] = Field(description="The labels of anything else it rests on (W, R, K, other events). Empty "
                                             "when it rests on its own events alone.")
    attach: List[str] = Field(description="The events (E) it comes from, kept as its evidence.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_decisions: List[EventDecision] = Field(description="Exactly one decision for every event given.")
    concern_updates: List[ConcernUpdate] = Field(default_factory=list)
    new_concerns: List[NewConcern] = Field(default_factory=list)
