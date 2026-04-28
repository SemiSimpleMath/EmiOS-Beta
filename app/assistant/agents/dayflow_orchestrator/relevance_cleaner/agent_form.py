from typing import List, Literal

from pydantic import BaseModel, Field


class CleanupDecision(BaseModel):
    item_id: str = Field(description="The short numeric task ID (e.g. '472'). Must match the 'task: <number>' shown in the item list.")
    action: Literal["close", "suppress"] = Field(
        description="close = mark as done (keeps history). suppress = permanently hide (irreversible).",
    )
    reason: str = Field(description="Short rationale for the decision.")


class AgentForm(BaseModel):
    cleanup_summary: str = Field(description="One-line summary of cleanup pass.")
    decisions: List[CleanupDecision] = Field(
        default_factory=list,
        description="Per-item cleanup decisions. Only include items you want to suppress; omit items you want to keep.",
    )
