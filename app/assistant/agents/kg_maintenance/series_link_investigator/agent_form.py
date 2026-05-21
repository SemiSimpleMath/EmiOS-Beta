from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


Verdict = Literal["approve", "reject"]


class ParticipantNote(BaseModel):
    """Aggregated cross-instance signal: a participant that appears
    on multiple Events in the cluster (suggests a concept-level fact)."""
    model_config = ConfigDict(extra="forbid")
    participant_label: str = Field(max_length=200)
    occurrences: int = Field(ge=1)


class AgentForm(BaseModel):
    """Investigator output for one candidate event-series cluster."""
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict = Field(
        ...,
        description=(
            "'approve' = mint instance_of edges (executor will create the "
            "parent Entity if none exists at canonical_label). 'reject' = "
            "leave the Events as-is; the cluster is not a real series."
        ),
    )

    canonical_label: str = Field(
        "",
        max_length=200,
        description=(
            "Concept name to use when verdict='approve'. May refine the "
            "triage's proposed label if a clearer name is warranted. Leave "
            "empty when verdict='reject'."
        ),
    )

    reasoning: str = Field(
        "",
        max_length=1000,
        description=(
            "2-4 sentence explanation. State the substance the concept "
            "holds beyond mere recurrence (or the absence of that substance "
            "when rejecting). Reference patterns observed across "
            "instances — participants, cadence, character."
        ),
    )

    cross_instance_participants: List[ParticipantNote] = Field(
        default_factory=list,
        description=(
            "Participants observed on 2+ Events in the cluster — concept-"
            "level facts the parent Entity could carry. Empty list is fine "
            "when none are apparent or when verdict='reject'."
        ),
    )

    cadence_note: str = Field(
        "",
        max_length=300,
        description=(
            "Brief note on observed cadence (e.g., 'weekly on Fridays', "
            "'monthly', 'irregular') if discernible from start_dates. "
            "Empty when not enough signal."
        ),
    )
