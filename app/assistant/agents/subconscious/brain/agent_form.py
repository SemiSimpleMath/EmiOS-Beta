"""Output schema for subconscious::brain — what one matter changes in the household's concerns."""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.agents.subconscious.noticer.agent_form import ConcernKind, Horizon, Severity


class EventDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(description="The event's label exactly as given, e.g. E2.")
    decision: Literal["used", "tracked_as_new", "needs_nothing"] = Field(
        description="used: it changes or confirms a concern you update (that update names it in relies_on or "
                    "attach). tracked_as_new: a new concern comes from it (that new concern names it). "
                    "needs_nothing: it changes no open concern and raises nothing new; the reason records what "
                    "it established.")
    reason: str = Field(description="Why it matters, or why it does not: specific to this event, in words; no "
                                    "labels.")


class ConcernUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concern: str = Field(description="The concern's label exactly as given, e.g. C1.")
    action: Literal["note", "resolve"] = Field(
        description="note: record what the events add (a new fact, a new date, a new plan, progress). "
                    "resolve: the concern is settled: its done-when is met, or it is cancelled or no "
                    "longer needed, or the owner says it is over.")
    note: str = Field(description="For note: the new facts, stated plainly. For resolve: why it is settled. "
                                  "Words and dates; no labels.")
    relies_on: List[str] = Field(description="The labels of what the note rests on: events (E), and anything else "
                                             "shown (W, M, P, K, S, R).")
    attach: List[str] = Field(description="The labels of what becomes part of this concern: events (E) whose message, "
                                          "email, sighting or work report is kept as its evidence, and work (W) whose "
                                          "later reports reach it. Empty when nothing is attached.")


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
    notes: str = Field(description="Why this matters to the household, and what is known so far, in words; no "
                                   "labels.")
    relies_on: List[str] = Field(description="The labels of anything else it rests on (W, M, P, K, S, R, other "
                                             "events). Empty when it rests on its own events alone.")
    attach: List[str] = Field(description="The labels of the events it comes from (E), kept as its evidence, and of "
                                          "work (W) that belongs to it.")
    owner_words: Optional[str] = Field(
        default=None,
        description="When the owner asked for this to be tracked or done later: the owner's words, copied "
                    "exactly from the event. Empty otherwise.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_decisions: List[EventDecision] = Field(description="Exactly one decision for every event given.")
    concern_updates: List[ConcernUpdate] = Field(default_factory=list)
    new_concerns: List[NewConcern] = Field(default_factory=list)
