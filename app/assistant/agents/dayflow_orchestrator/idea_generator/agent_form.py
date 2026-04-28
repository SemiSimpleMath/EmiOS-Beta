from typing import List

from pydantic import BaseModel, Field


class TriageAction(BaseModel):
    item_id: str = Field(default="", description="Optional related item id.")
    decision: str = Field(default="store_context_only", description="Decision label for compatibility.")
    reason: str = Field(default="", description="Short reason for decision.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence in [0.0, 1.0].")


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
    importance: str = Field(default="medium", description="Suggested importance label (e.g., low, medium, high).")
    actionability: str = Field(default="actionable", description="Suggested actionability label.")
    state: str = Field(default="new", description="Initial lifecycle state label for the spawned item.")
    state_reason: str = Field(
        default="spawned_by_idea_generator",
        description="Why this item was spawned now, with brief provenance from context evidence.",
    )


class AgentForm(BaseModel):
    triage_summary: str = Field(
        description="Short summary of idea generation decisions for this pass.",
    )
    triage_actions: List[TriageAction] = Field(
        default_factory=list,
        description="Compatibility field; typically empty for idea generation.",
    )
    spawn_items: List[SpawnItem] = Field(
        default_factory=list,
        description="Spawned idea items produced when no incoming items are present.",
    )
