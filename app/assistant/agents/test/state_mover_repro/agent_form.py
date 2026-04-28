from typing import List, Literal

from pydantic import BaseModel, Field, model_validator


class StateMutation(BaseModel):
    item_id: str = Field(description="Canonical task/item id being transitioned.")
    from_state: Literal[
        "new", "watching", "important_open", "waiting", "acted_on", "suppressed", "closed"
    ] = Field(description="Observed previous state.")
    to_state: Literal[
        "watching", "important_open", "waiting", "acted_on", "suppressed", "closed"
    ] = Field(description="Target lifecycle state.")
    reason: str = Field(description="Human-readable transition rationale.")
    wait_reason: str = Field(default="", description="Required when to_state=waiting.")
    reactivate_at: str = Field(default="", description="Local datetime wake time.")
    wake_signals: List[str] = Field(default_factory=list, description="Event wake signals.")
    priority_on_wake: str = Field(default="", description="Importance label on wake.")

    @model_validator(mode="after")
    def validate_waiting_rule(self):
        if self.to_state == "waiting":
            has_reactivate = bool(str(self.reactivate_at or "").strip())
            has_signals = bool(self.wake_signals)
            if not self.wait_reason.strip():
                raise ValueError("wait_reason is required when to_state=waiting.")
            if not (has_reactivate or has_signals):
                raise ValueError("to_state=waiting requires reactivate_at or wake_signals.")
        return self


class AgentForm(BaseModel):
    state_mover_summary: str = Field(description="Short summary of applied lifecycle moves.")
    state_mutations: List[StateMutation] = Field(
        default_factory=list,
        description="Lifecycle transitions to apply.",
    )

