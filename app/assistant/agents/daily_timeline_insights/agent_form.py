from typing import List, Optional

from pydantic import BaseModel, Field


class ExtractedClaim(BaseModel):
    """Structured, machine-resolvable form of an insight (belief-engine v2 ingestion seam, §3a).

    The phrases are PROPOSALS — a per-user concept registry canonicalizes them downstream; they are
    never used directly as identity keys (structured != canonical). applies_when is a soft, optional
    cue and is never forced; polarity is a proposal, not a verdict.
    """
    subject_phrase: str = Field(
        description="Who or what the claim is about, as a short PHRASE — e.g. 'the user', 'the kids', "
                    "'the household', 'the dog'. A natural phrase, not an id; it is canonicalized later."
    )
    predicate_phrase: str = Field(
        description="The relation as a phrase — e.g. 'prefers', 'avoids', 'does the routine', "
                    "'needs a reminder for', 'finds too frequent'."
    )
    object_phrase: str = Field(
        description="What the predicate applies to, as a phrase — e.g. 'standing-break nudges spaced "
                    "further apart', 'coffee before 11am', 'the evening dog walk'."
    )
    qualifiers: List[str] = Field(
        default_factory=list,
        description="Conditions that refine the claim, each a short phrase — e.g. 'during deep work', "
                    "'in the evening', 'unless hidden in pasta'. Empty list if none.",
    )
    applies_when: Optional[str] = Field(
        default=None,
        description="OPTIONAL plain-words cue for WHEN this applies — e.g. 'weekday mornings', "
                    "'after 16:00', 'first business day of the month'. Leave null when the claim is "
                    "not time- or context-bound; never invent one.",
    )
    polarity: str = Field(
        default="affirm",
        description="affirm = the claim holds; contradict = evidence AGAINST a previously-held claim "
                    "(the user changed their mind); retract = the user explicitly withdrew it. "
                    "Default 'affirm'.",
    )
    kind: Optional[str] = Field(
        default=None,
        description="Memory class — drives how the belief engine forgets. One of: "
                    "'semantic_fact' (stable world-fact about the user/household — a health "
                    "condition, a child's school), 'preference' (likes/dislikes/wants — both never "
                    "decay from silence), 'procedural_routine' (a recurring practice — weekly "
                    "call, monthly timesheet — decays only when expected occurrences are missed), "
                    "'episodic' (tied to a specific situation or period — during an illness, while "
                    "on a trip — fades over weeks), 'transient_state' (true right now, gone in "
                    "days — feeling sick today). When unsure choose the LONGER-lived class.",
    )
    llm_interpretation: Optional[str] = Field(
        default=None,
        description="One short line: what this claim means for how the assistant should behave.",
    )


class ActionableItem(BaseModel):
    fact_summary: str = Field(description="Concise actionable insight.")
    tags: List[str] = Field(description="Tags for routing to resource files.")
    change_recommended: str = Field(description="Specific change or recommendation.")
    evidence: List[str] = Field(
        default_factory=list,
        description="Short evidence strings or ids from the timeline.",
    )
    temporal_scope: Optional[str] = Field(
        default="chronic",
        description="chronic | daily | historical",
    )
    extracted_claim: Optional[ExtractedClaim] = Field(
        default=None,
        description="The same insight as a structured subject/predicate/object claim (+ optional "
                    "qualifiers, applies_when cue, and polarity) for the belief engine. fact_summary "
                    "stays the human-readable version; populate this for every item when you can.",
    )


class AgentForm(BaseModel):
    actionable_information: List[ActionableItem]
