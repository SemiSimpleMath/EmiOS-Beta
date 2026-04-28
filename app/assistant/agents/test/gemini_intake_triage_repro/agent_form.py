from typing import List

from pydantic import BaseModel, Field


class TriageAction(BaseModel):
    item_id: str = Field(description="Canonical item id being triaged.")
    decision: str = Field(
        description=(
            "Triage decision. Must be one of: "
            "ignore_suppress, store_context_only, create_actionable, "
            "update_actionable, escalate_actionable, resolve_actionable."
        )
    )
    reason: str = Field(description="Short reason for this decision.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score in [0.0, 1.0].",
    )


class SpawnItem(BaseModel):
    item_id: str = Field(
        default="",
        description="Optional stable id for the spawned item. Leave empty if unknown.",
    )
    summary: str = Field(
        description=(
            "Human-readable, explicit operator instruction. "
            "Must be specific enough to be actionable out of context."
        )
    )
    source_type: str = Field(description="Source category for spawned item (usually system_event).")
    event_type: str = Field(description="Event type label for spawned item.")
    importance: str = Field(
        default="medium",
        description="Suggested importance label (e.g., low, medium, high).",
    )
    actionability: str = Field(
        default="actionable",
        description="Suggested actionability label (e.g., actionable, context_only).",
    )
    state: str = Field(
        default="new",
        description="Initial lifecycle state label for the spawned item.",
    )
    state_reason: str = Field(
        default="spawned_by_triage",
        description=(
            "Why this item was spawned now, with brief provenance from input context "
            "(routine/calendar/chat/ticket/schedule-change evidence)."
        ),
    )


class AgentForm(BaseModel):
    triage_summary: str = Field(
        description="Short summary of triage decisions for this pass.",
    )
    triage_actions: List[TriageAction] = Field(
        default_factory=list,
        description="Structured triage decisions for incoming items.",
    )
    spawn_items: List[SpawnItem] = Field(
        default_factory=list,
        description="Structured spawned items derived from context.",
    )


