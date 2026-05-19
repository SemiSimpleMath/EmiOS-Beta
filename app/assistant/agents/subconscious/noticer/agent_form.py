"""Output schema for the subconscious noticer.

The noticer reads household signals and emits a structured update to the
concerns_register, plus belief updates, plus optional user-facing questions.
It does NOT act on the world directly — only delegates by labeling
concerns with addressable_by.

See `project_subconscious_brain_architecture` memory for the full design.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


EvidenceKind = Literal[
    "chat_msg",
    "diet_log_entry",
    "sleep_log_entry",
    "activity_log_entry",
    "calendar_event",
    "kg_fact",
    "pod",
    "dayflow_item",
    "observation",
]


ConcernKind = Literal[
    "pattern_drift",
    "anticipated_need",
    "opportunity_external",
    "opportunity_internal",
    "schedule_collision",
    "gift_opportunity",
]


Severity = Literal["low", "medium", "high"]
Horizon = Literal["today", "this_week", "this_month", "long_horizon"]
Urgency = Literal["medium", "high"]
SeverityChange = Literal["raised", "lowered"]
Polarity = Literal["support", "contradict"]
Confidence = Literal["low", "medium", "high"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EvidenceKind
    ref: str = Field(description="ID, path, or pod_id of the real underlying item.")
    snippet: Optional[str] = Field(default=None, description="Short excerpt for human inspection.")


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concern_id: str = Field(description="UUID for this concern. The noticer generates it.")
    title: str = Field(max_length=120, description="One line, specific.")
    subject: Optional[str] = Field(default=None, description="Family member name or 'household'.")
    kind: ConcernKind
    domain_tags: List[str]
    severity: Severity
    horizon: Horizon
    evidence: List[Evidence]
    addressable_by: List[str] = Field(
        description="Which proposers/surfaces can deliver. e.g. ['meal_proposer','chat_brain']."
    )
    notes: str = Field(max_length=600, description="Reasoning. Why this matters. NOT a proposal.")
    first_observed: str = Field(description="ISO datetime when this concern was first crystallized.")


class ConcernReinforcement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concern_id: str = Field(description="Existing concern_id this reinforces.")
    new_evidence: List[Evidence]
    severity_change: Optional[SeverityChange] = None
    notes: Optional[str] = Field(default=None, max_length=300)


class ConcernResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concern_id: str
    reason: str = Field(max_length=300, description="Why this is now resolved.")
    evidence: List[Evidence] = Field(description="Items showing the signal has stopped.")


class ConcernEscalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concern_id: str
    target: Literal["dayflow_orchestrator"]
    urgency: Urgency
    reason: str = Field(max_length=300)


class BeliefUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    belief_kind: str = Field(
        description=(
            "Belief category. e.g. 'subconscious_meal_preference', "
            "'household_routine_pattern', 'family_member_state', "
            "'subconscious_outward_preference'."
        )
    )
    subject: Optional[str] = Field(default=None, description="Family member or 'household'.")
    claim: str = Field(max_length=300, description="The proposition.")
    polarity: Polarity
    evidence: List[Evidence]
    related_concern_id: Optional[str] = None
    confidence: Confidence
    half_life_days: Optional[int] = Field(
        default=None,
        description="Noticer's guess for how fast this should decay. Belief store uses kind-driven default if None.",
    )


class PendingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(description="UUID for tracking.")
    text: str = Field(max_length=400, description="The question to ask the user.")
    related_concern_id: Optional[str] = None
    why_asking: str = Field(max_length=300, description="What signal prompted this.")
    if_unanswered: str = Field(
        max_length=300,
        description="Default assumption the noticer proceeds with if the user doesn't reply.",
    )


class AgentForm(BaseModel):
    """Top-level noticer output — the agent runtime expects this exact name."""
    model_config = ConfigDict(extra="forbid")
    new_concerns: List[Concern] = Field(default_factory=list)
    reinforced_concerns: List[ConcernReinforcement] = Field(default_factory=list)
    resolved_concerns: List[ConcernResolution] = Field(default_factory=list)
    escalated_concerns: List[ConcernEscalation] = Field(default_factory=list)
    belief_updates: List[BeliefUpdate] = Field(default_factory=list)
    pending_questions: List[PendingQuestion] = Field(default_factory=list)
    summary: str = Field(
        max_length=800,
        description="2-4 sentences on what was noticed this tick and what changed in concerns_register.",
    )
    skipped_pass_b: bool = Field(
        description="True if outward opportunity scouting was skipped this tick."
    )
    skipped_pass_b_reason: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Required if skipped_pass_b is true.",
    )
