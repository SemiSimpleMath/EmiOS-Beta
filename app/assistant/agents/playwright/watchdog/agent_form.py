from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """
    Structured output for the Playwright watchdog.

    NOTE: This agent is not a planner. It should NOT output `action` tool calls.
    It may set `next_agent` to `critic_capture_node` to force a critic gate.
    """

    watchdog_in_trouble: bool = Field(
        default=False,
        description="True if the planner appears stuck/looping or a blocker likely exists.",
    )
    watchdog_reason: str = Field(
        default="",
        description="Short reason explaining why watchdog thinks we're in trouble (or empty if fine).",
    )

    # Flow signal: if set, the delegator will respect and route there.
    next_agent: str | None = Field(
        default=None,
        description="Set to 'critic_capture_node' to force a critic run; otherwise null.",
    )

    playwright_critic_trigger_reason: str | None = Field(
        default=None,
        description="Optional reason string for debugging critic triggers.",
    )

