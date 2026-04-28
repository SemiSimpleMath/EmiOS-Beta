from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class PlannedToolCall(BaseModel):
    tool: str = Field(description="Tool name from the catalog.")
    args_json: str = Field(
        default="{}",
        description='Arguments as a JSON object string, e.g. {"url": "https://www.cnn.com"}. Use ${artifact_N} for prior step data.',
    )
    purpose: str = Field(
        default="",
        description="One-line description of what this tool call accomplishes.",
    )


class ProducedOutput(BaseModel):
    id: str = Field(description="Output id, e.g. artifact_1 or fact_1.")
    kind: Literal["artifact", "fact"] = Field(
        default="artifact",
        description="artifact = rich content (summaries, screenshots). fact = scalar (boolean, number, string).",
    )
    description: str = Field(description="What this output contains.")


class WaitSpec(BaseModel):
    wait_type: Literal["time", "duration", "event", "user_reply"] = Field(
        description="What kind of wait.",
    )
    description: str = Field(description="Human description of wait condition.")
    time_local: str = Field(default="", description="Local time to wait until (e.g., '09:00').")
    duration_minutes: int = Field(default=0, description="Minutes to wait.")
    event_name: str = Field(default="", description="Canonical event name.")


class DecisionSpec(BaseModel):
    condition: str = Field(description="Condition to evaluate (e.g., 'artifact_1 contains urgent items').")
    description: str = Field(description="Human description of the decision.")
    on_true_step: str = Field(default="", description="Step name to go to if true.")
    on_false_step: str = Field(default="", description="Step name to go to if false.")


class StepPlan(BaseModel):
    """Complete execution plan for one step (may merge multiple source steps)."""
    step_name: str = Field(description="Name of the planned step.")

    source_steps: List[int] = Field(
        default_factory=list,
        description="1-based indices of the original input steps this plan covers. "
                    "Usually [N] for a single step, or [N, N+1] when consecutive steps are merged.",
    )

    # Execution type
    kind: Literal["tool_sequence", "action", "wait", "decision"] = Field(
        description=(
            "tool_sequence = deterministic tool calls (fastest). "
            "action = LLM-driven manager execution. "
            "wait = pause for time/event/user. "
            "decision = branch based on condition."
        ),
    )

    # For action steps
    manager_name: str = Field(
        default="",
        description="Manager name for action steps. Empty for other kinds.",
    )
    instruction: str = Field(
        default="",
        description="Natural language instruction for the manager. Derived from step description.",
    )
    pinned_tools: List[str] = Field(
        default_factory=list,
        description="Tools the manager will need for this step. These are pinned as visible, bypassing the narrower. Only include tools the step explicitly requires.",
    )

    # For tool_sequence steps
    tools: List[PlannedToolCall] = Field(
        default_factory=list,
        description="Ordered tool calls for deterministic steps.",
    )

    # For wait steps
    wait: Optional[WaitSpec] = Field(
        default=None,
        description="Wait condition. Only for wait steps.",
    )

    # For decision steps
    decision: Optional[DecisionSpec] = Field(
        default=None,
        description="Branch condition. Only for decision steps.",
    )

    # Data flow
    produces: List[ProducedOutput] = Field(
        default_factory=list,
        description="What this step produces. Most steps produce one artifact.",
    )
    consumes: List[str] = Field(
        default_factory=list,
        description="Data ids consumed from prior steps (e.g., ['artifact_1', 'artifact_2']).",
    )

    # Reasoning
    reasoning: str = Field(
        default="",
        description="Brief explanation of the execution strategy chosen.",
    )


class AgentForm(BaseModel):
    step_plans: List[StepPlan] = Field(
        default_factory=list,
        description="One plan per output step. May be fewer than input steps if consecutive same-executor steps are merged. "
                    "Each plan's source_steps field tracks which input steps it covers.",
    )
