"""Output schema for feedback_extractor.

Reads user comments on subconscious suggestions (intention.* pods) and
extracts belief updates that BeliefStore.upsert_belief can ingest.

For each comment processed, the agent may emit 0-N extractions:
- 0 when the comment is ambiguous, sentiment-only, or doesn't imply a
  preference (e.g., "hmm" / "lol" / "ok").
- 1 when the comment expresses a single calibration.
- 2+ when one comment expresses multiple preferences ("less of X, more
  of Y" → two extractions).

The extractor's confidence calibration is the load-bearing tuning
knob — see system.j2 for the contract.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


BeliefDomain = Literal[
    "meal",
    "wellness",
    "romantic",
    "scheduling",
    "communication",
    "household_routine",
    "other",
]
Confidence = Literal["high", "medium", "low"]
Scope = Literal["chronic", "temporary"]
SignalType = Literal["confirms", "qualifies", "contradicts", "rejects"]


class BeliefExtraction(BaseModel):
    """One belief implied by one user comment."""
    model_config = ConfigDict(extra="forbid")
    source_comment_pod_id: str = Field(
        description="The feedback.comment pod_id this extraction was derived from.",
    )
    target_pod_id: str = Field(
        description="The intention.* / plan.weekly_schedule pod_id the comment was attached to.",
    )
    domain: BeliefDomain
    belief_key: str = Field(
        max_length=200,
        description=(
            "Deterministic dot-separated key for upsert. Shape: "
            "<domain>.<sub-area>.<specifier>.<value>. Examples: "
            "'romantic.date_night.frequency.prefer_less', "
            "'meal.flavors.spice.prefer_mild', "
            "'scheduling.tuesday.avoid_evening_events'. "
            "Reuse an existing belief_key when the comment reinforces or "
            "contradicts a belief already in recent_existing_beliefs."
        ),
    )
    statement: str = Field(
        max_length=500,
        description=(
            "Human-readable proposition. Should be a complete sentence "
            "that captures what the household prefers/wants. NOT a "
            "restatement of the comment itself."
        ),
    )
    signal_type: SignalType = Field(
        description=(
            "How this comment affects the belief: confirms (positive "
            "support), qualifies (adds nuance/conditions), contradicts "
            "(negative evidence against the belief), rejects (strong "
            "veto of the belief). Pick contradicts/rejects when the "
            "comment is a veto, confirms when it's appreciation, "
            "qualifies when it's calibration."
        ),
    )
    confidence: Confidence = Field(
        description=(
            "How strongly this comment supports the belief. high = the "
            "comment is explicit and unambiguous ('we don't want X'). "
            "medium = clearly implied but could be situational. low = "
            "weakly suggested or one-off mood."
        ),
    )
    scope: Scope = Field(
        description=(
            "chronic = ongoing household preference. temporary = "
            "situational (this week, this trip, etc.)."
        ),
    )
    reasoning: str = Field(
        max_length=400,
        description=(
            "Why this comment maps to this belief. Helps audit the "
            "extraction's quality post-hoc."
        ),
    )


class CommentSkipped(BaseModel):
    """A comment the extractor read but produced 0 extractions from."""
    model_config = ConfigDict(extra="forbid")
    comment_pod_id: str
    reason: str = Field(max_length=300)


class AgentForm(BaseModel):
    """Top-level feedback_extractor output."""
    model_config = ConfigDict(extra="forbid")
    extractions: List[BeliefExtraction] = Field(default_factory=list)
    skipped: List[CommentSkipped] = Field(
        default_factory=list,
        description=(
            "Comments that produced 0 extractions, with reasons. Makes "
            "the extractor's silence auditable."
        ),
    )
    free_form_thinking: str = Field(max_length=1000)
