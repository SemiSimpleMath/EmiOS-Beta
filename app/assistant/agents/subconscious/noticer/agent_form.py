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


class ConcernAddressing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concern_id: str
    notes: Optional[str] = Field(
        default=None, max_length=300,
        description="What was already done about it (e.g. dayflow researched it / asked the user).",
    )
    evidence: List[Evidence] = Field(
        description="Items showing it is being handled internally (e.g. dayflow action results)."
    )


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


class ConcernDisposition(BaseModel):
    """The forced decision on a long-running concern.

    The context lists concerns UNDER PRESSURE (reinforced many times since
    the last decision, or stale in `addressing`). Each one MUST get exactly
    one disposition this tick:
    - accept_chronic: a real but long-term pattern; archive to dormant with
      a summary so it stops consuming ticks (it can be re-raised as a NEW
      concern if it sharpens).
    - re_escalate: the handoff stalled or the situation worsened; push it
      back to active with a fresh high-urgency escalation.
    - keep_active: justified continuation — say WHY tracking longer is the
      right call; the pressure rule resets and won't re-fire for a while.
    """
    model_config = ConfigDict(extra="forbid")
    concern_id: str
    action: Literal["accept_chronic", "re_escalate", "keep_active"]
    reason: str = Field(max_length=300, description="Why this disposition is right.")


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


class QuestionOutcome(BaseModel):
    """Processing verdict for a question in the mailbox.

    Required for every question the context lists under 'Question mailbox'.
    `processed` means the captured answer was read AND its consequences
    were emitted in this same tick (concern reinforce/resolve/disposition,
    or a belief update). `expired_default_applied` retires a stale
    unanswered question — apply the default the question itself stated.
    """
    model_config = ConfigDict(extra="forbid")
    question_id: str
    outcome: Literal["processed", "expired_default_applied"]
    notes: str = Field(
        max_length=300,
        description="What the answer (or default) changed in the register.",
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
    addressing_concerns: List[ConcernAddressing] = Field(
        default_factory=list,
        description=(
            "Concerns already being handled internally (e.g. the dayflow orchestrator researched "
            "or acted on it) but not yet resolved. Moves them out of `active` so they stop nagging "
            "the planner, while keeping them tracked. Use instead of reinforcing when you see from "
            "recent dayflow outcomes that the concern is being worked, not ignored."
        ),
    )
    resolved_concerns: List[ConcernResolution] = Field(default_factory=list)
    escalated_concerns: List[ConcernEscalation] = Field(default_factory=list)
    concern_dispositions: List[ConcernDisposition] = Field(
        default_factory=list,
        description=(
            "REQUIRED for every concern the context lists under 'CONCERNS UNDER "
            "PRESSURE' — accept_chronic / re_escalate / keep_active with a reason. "
            "Empty when no concern is under pressure."
        ),
    )
    belief_updates: List[BeliefUpdate] = Field(default_factory=list)
    pending_questions: List[PendingQuestion] = Field(default_factory=list)
    question_outcomes: List[QuestionOutcome] = Field(
        default_factory=list,
        description=(
            "REQUIRED for every item in the context's 'Question mailbox' section. "
            "Empty when the mailbox is empty."
        ),
    )
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
