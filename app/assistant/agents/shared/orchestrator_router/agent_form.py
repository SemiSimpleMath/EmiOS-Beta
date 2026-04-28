from pydantic import BaseModel, ConfigDict, Field


class BroadcastItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(
        ...,
        description="Exact job_id of a running job (preferred), or type:<child_type> or child_id:<substring>.",
    )
    message: str = Field(..., description="Short factual update or constraint to forward.")


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_router`."""

    model_config = ConfigDict(extra="forbid")

    # Keep this simple (avoid Literal) to reduce pydantic/dynamic-import edge cases.
    action: str = Field("done", description="Always 'done'.")
    replan_needed: bool = Field(
        False,
        description="True if new work should be scheduled (advance stage, spawn missing job, or major change).",
    )
    cancel_running: bool = Field(
        False,
        description="If true, request cooperative cancellation of running children (best-effort).",
    )
    cancel_targets: list[str] = Field(
        default_factory=list,
        description="Optional: restrict cancellations to these job_id values.",
    )
    broadcast: list[BroadcastItem] = Field(
        default_factory=list,
        description="Best-effort forwarding: who should be informed of facts/constraints.",
    )
    notes: str = Field("", description="Short explanation for routing decisions.")


AgentForm.model_rebuild()

