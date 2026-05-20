"""Output schema for scheduler_arbiter.

The arbiter reads candidate intention.* pods from all proposers for the
next 14 days and emits THE authoritative weekly schedule. Conflicts are
either resolved here (LLM judgment + priority rules) or punted to the
user via a dayflow ticket.

Output is one plan.weekly_schedule pod per run. The proposers' next
runs read that pod as gospel and plan around it.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


SchedulingDomain = Literal["meal", "wellness", "romantic", "external", "family"]


class ScheduledItem(BaseModel):
    """One slot the arbiter committed to this week."""
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="ISO date (YYYY-MM-DD).")
    proposed_start_local: Optional[str] = Field(
        default=None,
        description="ISO datetime with tz for time-bound slots. Null for flex / same-day.",
    )
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=4320)
    actors: List[str]
    domain: SchedulingDomain = Field(
        description="Which proposer's domain this slot came from.",
    )
    summary: str = Field(max_length=200)
    source_intention_pod_id: Optional[str] = Field(
        default=None,
        description=(
            "The intention.* pod this slot came from. Allows the proposer "
            "to know its proposal was accepted; allows the schedule pod to "
            "trace back to source reasoning."
        ),
    )
    is_anchor: bool = Field(
        default=False,
        description=(
            "True = this slot is LOCKED for the week. Other proposers must "
            "plan around it. False = scheduled but flex (can move if needed)."
        ),
    )


class ConflictResolution(BaseModel):
    """A conflict the arbiter resolved on its own (no user input needed)."""
    model_config = ConfigDict(extra="forbid")
    conflict_summary: str = Field(
        max_length=300,
        description="One-line description. e.g. 'Wed dinner: date_night_out vs flex family meal'.",
    )
    chosen_pod_id: str = Field(description="The intention pod that won.")
    displaced_pod_ids: List[str] = Field(default_factory=list)
    reasoning: str = Field(max_length=500)
    priority_rule_applied: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Which rule from priority_rules drove this — or 'none, contextual judgment'.",
    )


class ConflictForUser(BaseModel):
    """A conflict the arbiter declined to resolve. Surfaces as a ticket."""
    model_config = ConfigDict(extra="forbid")
    conflict_summary: str = Field(max_length=300)
    options: List[str] = Field(
        description="2-4 human-readable options to present to the user.",
    )
    candidate_pod_ids: List[str]
    reasoning_for_punt: str = Field(
        max_length=400,
        description="Why this needs the user. e.g. 'Both anchors with equal weight; user preference needed.'",
    )


class AgentForm(BaseModel):
    """Top-level scheduler_arbiter output."""
    model_config = ConfigDict(extra="forbid")
    week_start_date: str = Field(description="ISO Monday date of the week this schedule covers.")
    weekly_schedule: List[ScheduledItem]
    conflicts_resolved: List[ConflictResolution] = Field(default_factory=list)
    conflicts_for_user: List[ConflictForUser] = Field(default_factory=list)
    free_form_thinking: str = Field(max_length=1500)
