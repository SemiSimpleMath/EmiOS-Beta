"""Output schema for subconscious::brief — the brief everyone working on a concern reads first."""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(description="One thing known about the matter, stated plainly.")
    source: str = Field(description="Where it comes from, as shown in the context: an evidence ref, a work id "
                                    "(work_...), a calendar:... anchor or a reminder:... ref; several separated by ';'. "
                                    "For a fact from the concern's own journal or notes, or from the knowledge graph's "
                                    "description of a person or place: journal, notes or knowledge graph.")


class Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["act_now", "hold", "no_action"] = Field(
        description="act_now: something should be done about this today; it is handed to the planner now. "
                    "hold: nothing to do until a date; it is looked at again then. no_action: nothing needs "
                    "doing (already handled, work in progress, the owner has it in hand, or it is only to know).")
    hold_until: Optional[str] = Field(
        default=None, description="For hold: the local date and time to look at it again, as YYYY-MM-DD HH:MM. "
                                  "Empty otherwise.")
    task: str = Field(description="For act_now: the task for the planner, broad, stating the outcome wanted, not "
                                  "the steps. Empty otherwise.")
    why: str = Field(description="Why this decision now, in one or two sentences.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(description="What the matter is, in two or three sentences, for someone new to it.")
    why_it_matters: str = Field(description="Why it matters to the household: what happens if it is missed.")
    known: List[Fact] = Field(description="What is known: dates, places, people, amounts, the current state.")
    tried: str = Field(description="What has already been done about it and how each attempt went. "
                                   "'Nothing yet' when nothing was done.")
    owner_wishes: str = Field(description="What the owner has said about it: wants, preferences, refusals, in the "
                                          "owner's words where given. 'Nothing said' when the owner said nothing.")
    depends_on_it: List[str] = Field(description="Items already in motion that depend on this matter: a reminder, a "
                                                 "calendar entry, active work, an arrangement with someone. Empty when none.")
    open_questions: List[str] = Field(description="What is not known and would change what to do. Empty when none.")
    recommendation: str = Field(description="The broad next step and why, or no action and why (already handled, "
                                            "the owner already knows, nothing to do until a date). Not a plan of steps.")
    readiness: Readiness = Field(description="Whether to act on it today, hold it until a date, or take no action.")
